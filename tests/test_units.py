import tempfile
from pathlib import Path

import pytest

from conftest import CASES, EXAMPLE, FIX, assert_same, lutra_lint, needs_lutra
from ottr_tarql import Library, Unsupported, compose, decompose, parse_query, parse_stottr, serialize_query
from ottr_tarql.cli import load_library
from ottr_tarql.ottr import render_document
from ottr_tarql.runner import expand, generate_instances, read_csv, run_query
from ottr_tarql.sparql import Bind, Raw
from ottr_tarql.terms import IRI, NONE, OTTR_TRIPLE as TRIPLE, TQ, XSD, BNode, ListTerm, Literal, Var

TPL = "http://example.org/ottr/template/"
QNS = "http://example.org/ottr/query/"
EX_NS = "http://example.com/ns#"


def _binds(root):
    return [a for a in root.annotations if a.template == TQ + "Bind"]


def test_parse_construct_forms():
    q = parse_query(
        """
        PREFIX : <http://ex/>   # comment
        construct {
            ?s a :T ;   # trailing comment
               :p "x"@en, 'y', 3, 4.5, true, "d"^^<http://ex/dt> ;
               :q [ :r ?o ] ;
               .
        } WHERE { BIND(IRI(?a) AS ?s) FILTER(?a < "z") BIND(  1+2 AS ?o ) }
        LIMIT 10
        """
    )
    assert (Var("s"), IRI("http://www.w3.org/1999/02/22-rdf-syntax-ns#type"), IRI("http://ex/T")) in q.triples
    objs = {o for s, p, o in q.triples if p == IRI("http://ex/p")}
    assert objs == {
        Literal("x", lang="en"),
        Literal("y"),
        Literal("3", XSD + "integer"),
        Literal("4.5", XSD + "decimal"),
        Literal("true", XSD + "boolean"),
        Literal("d", "http://ex/dt"),
    }
    assert (BNode("anon1"), IRI("http://ex/r"), Var("o")) in q.triples
    assert q.where == [Bind("s", "IRI(?a)"), Raw('FILTER(?a < "z")'), Bind("o", "1+2")]
    assert q.tail == "LIMIT 10"
    # serialization re-parses to the same structure
    q2 = parse_query(serialize_query(q))
    assert set(q2.triples) == set(q.triples) and q2.where == q.where and q2.tail == q.tail


@pytest.mark.parametrize("construct", ["?s :p ?o ~ _:r .", "?s :p ?o {| :src ?x |} .", "<< ?s :p ?o >> :src ?x ."])
def test_rdf12_syntax_is_rejected(construct):
    """The project works with RDF 1.1 and SPARQL 1.1; RDF 1.2 input is refused with a clear message."""
    with pytest.raises(Unsupported, match="RDF 1.1"):
        parse_query(f"PREFIX : <http://ex/> CONSTRUCT {{ {construct} }} WHERE {{}}")


def test_collections_in_construct_are_rejected():
    with pytest.raises(Unsupported):
        parse_query("PREFIX : <http://ex/> CONSTRUCT { ?s :p (1 2) . } WHERE {}")


def test_stottr_roundtrip():
    text = (FIX / "extra" / "products.stottr").read_text(encoding="utf8")
    doc = parse_stottr(text)
    again = parse_stottr(render_document(list(doc.templates.values()), doc.prefixes))
    assert again.templates == doc.templates
    product = doc.templates["http://example.com/templates#Product"]
    cross = product.body[1]
    assert cross.expander == "cross" and cross.expand_flags == [False, True]
    assert isinstance(cross.args[1], ListTerm)
    assert product.param("category").default == IRI("http://example.com/ns#Misc")


def test_stottr_term_syntax():
    doc = parse_stottr(
        """@prefix ex: <http://ex/> .
        ex:T [ !? LUB<ex:C> ?a, List<NEList<ex:D>> ?b ] .
        ex:T(none, ((1, "s"), [], _:x, -2.5e1)) .
        zipMax | ex:U(++(ex:a), ?v) ."""
    )
    sig = doc.templates["http://ex/T"]
    assert sig.kind == "signature" and sig.params[0].optional and sig.params[0].nonblank
    i = doc.instances[0]
    assert i.args[0] is NONE
    assert i.args[1].items[3] == Literal("-2.5e1", XSD + "double")
    assert doc.instances[1].expander == "zipMax"


def _q(name, body, where=""):
    return parse_query(f"PREFIX : <http://ex/> CONSTRUCT {{ {body} }} WHERE {{ {where} }}", name)


def test_identical_shapes_are_shared_across_queries():
    d = decompose(
        [
            _q("a", "?x a :Person ; :name ?n .", "BIND(IRI(?id) AS ?x)"),
            _q("b", "?who :name ?label ; a :Person .", "BIND(IRI(?k) AS ?who)"),
        ]
    )
    assert [t.iri for t in d.library] == [TPL + "Person"]
    assert [i.template for r in d.roots for i in r.body] == [TPL + "Person", TPL + "Person"]


def test_types_distinguish_shapes():
    d = decompose(
        [
            _q("a", "?x :size ?n .", "BIND(IRI(?id) AS ?x)"),
            _q("b", "?x :size ?n .", "BIND(IRI(?id) AS ?x) BIND(xsd:integer(?v) AS ?n)"),
        ]
    )
    assert len(d.library) == 2


def test_factoring_reuses_smaller_shape():
    d = decompose(
        [
            _q("small", "?x a :T ; :p ?a .", "BIND(IRI(?i) AS ?x)"),
            _q("big", "?x a :T ; :p ?a ; :q ?b .", "BIND(IRI(?i) AS ?x)"),
        ]
    )
    small, big = (next(t for t in d.library if t.iri == TPL + n) for n in ("T", "T_2"))
    assert big.body[0].template == small.iri
    assert [i.template for i in big.body[1:]] == ["http://ns.ottr.xyz/0.4/Triple"]
    unfactored = decompose([_q("small", "?x a :T ; :p ?a ."), _q("big", "?x a :T ; :p ?a ; :q ?b .")], factor=False)
    assert all(i.template.endswith("Triple") for t in unfactored.library for i in t.body)


def test_shared_blank_node_is_created_by_root():
    d = decompose([_q("q", "?x :addr _:a . _:a :city ?c .", "BIND(IRI(?id) AS ?x)")])
    root = d.roots[0]
    assert any(isinstance(a, BNode) for i in root.body for a in i.args)


def test_predicate_variable_is_nonblank():
    d = decompose([_q("q", "?x ?p ?o .", "BIND(IRI(?i) AS ?x) BIND(IRI(?pp) AS ?p)")])
    assert d.roots[0].param("p").nonblank
    assert d.library[0].params[1].nonblank


@pytest.mark.parametrize(
    "expr, expected",
    [
        ("COALESCE(tarql:expandPrefixedName(?c), IRI(?c))", "http://ns.ottr.xyz/0.4/IRI"),
        ("COALESCE(tarql:expandPrefixedName(?c), :default)", "http://ns.ottr.xyz/0.4/IRI"),
        ('IF(CONTAINS(STR(?c), "://"), IRI(?c), tarql:expandPrefixedName(?c))', "http://ns.ottr.xyz/0.4/IRI"),
        ("COALESCE(xsd:integer(?n), 0)", XSD + "integer"),
        ("COALESCE(xsd:integer(?n), 0.5)", None),  # the branches disagree
        ("IF(BOUND(?a), :Class, ?none)", None),  # ?none could be a column holding a string
        ("IF(BOUND(?a), ?x, ?y)", XSD + "decimal"),  # through the BINDs of ?x and ?y
    ],
)
def test_types_through_coalesce_and_if(expr, expected):
    q = _q("q", "?s :p ?v .", f"BIND(xsd:decimal(?d) AS ?x) BIND(xsd:decimal(?e) AS ?y) BIND({expr} AS ?v)")
    q.prefixes.add("xsd", XSD)
    assert decompose([q]).roots[0].param("v").type == expected


def test_compose_guards_come_back_as_mandatory_parameters():
    """OTTR -> TARQL -> OTTR: the guards on ex:ReportsTo's triples become a nested
    template with ?manager mandatory and constant predicates, meaning the same."""
    lib = Library([parse_stottr((EXAMPLE / "people.stottr").read_text(encoding="utf8"))])
    composed = compose(lib, "ex:Person").query
    assert any(isinstance(p, Var) for _, p, _ in composed.triples)
    d = decompose([parse_query(serialize_query(composed), "person")])
    assert not any(isinstance(i.args[1], Var) for t in d.library for i in t.body if i.template == TRIPLE)
    assert not any(a.args[1].lexical.startswith("IF(sameTerm") for a in _binds(d.roots[0]))
    reports = next(t for t in d.library if any(i.args[1] == IRI(EX_NS + "reportsTo") for i in t.body))
    assert not reports.param("reportsTo").optional
    rows = read_csv((EXAMPLE / "people.csv").read_text(encoding="utf8"))
    with tempfile.TemporaryDirectory() as tmp:
        d.write(tmp)
        back = load_library([tmp])
    assert_same(run_query(composed, rows), run_query(compose(back, QNS + "person").query, rows))
    assert_same(run_query(composed, rows), expand(back, generate_instances(back, QNS + "person", rows)))


def test_guard_lookalike_used_elsewhere_is_left_alone():
    q = _q("q", "?x ?g ?o . ?x :also ?g .", "BIND(IRI(?i) AS ?x) BIND(IF(sameTerm(?o, ?o), :p, ?_unbound) AS ?g)")
    d = decompose([q])
    assert d.roots[0].param("g") is not None
    assert [a.args[0].lexical for a in _binds(d.roots[0])] == ["x", "g"]


def test_list_parameter_cannot_be_composed():
    lib = Library(
        [
            parse_stottr(
                """@prefix ex: <http://ex/> . @prefix ottr: <http://ns.ottr.xyz/0.4/> .
                ex:T [ ottr:IRI ?x, List<ottr:IRI> ?ys ] :: { cross | ottr:Triple(?x, ex:p, ++?ys) } ."""
            )
        ]
    )
    with pytest.raises(Unsupported):
        compose(lib, "ex:T")


def test_compose_without_inferred_binds_uses_columns_directly():
    lib = Library(
        [
            parse_stottr(
                """@prefix ex: <http://ex/> . @prefix ottr: <http://ns.ottr.xyz/0.4/> .
                ex:T [ ottr:IRI ?x, ? ?label ] :: { ottr:Triple(?x, ex:label, ?label) } ."""
            )
        ]
    )
    res = compose(lib, "ex:T", infer_binds=False)
    assert res.query.where == []
    assert res.query.triples == [(Var("x"), IRI("http://ex/label"), Var("label"))]
    assert res.columns == ["x", "label"]


@needs_lutra
def test_generated_library_lints_clean(tmp_path):
    queries = [parse_query((FIX / c).read_text(encoding="utf8"), Path(c).stem) for c in CASES]
    decompose(queries).write(tmp_path)
    report = lutra_lint(tmp_path)
    assert "WARNING" not in report and "ERROR" not in report, report

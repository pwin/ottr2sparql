"""Semantic equivalence checks.

For every TARQL fixture, on its CSV:
  run(original query) == run(compose(decompose(query)))         TARQL -> OTTR -> TARQL
  run(original query) == Lutra(instances of the root template)   OTTR templates mean the same
For hand-written OTTR templates:
  Lutra(instances) == run(compose(template))                     OTTR -> TARQL
"""

from pathlib import Path

import pytest
import rdflib
from rdflib.compare import isomorphic, to_isomorphic, graph_diff

from conftest import CASES, FIX, lutra_expand, needs_lutra
from ottr_tarql import Library, compose, decompose, parse_query, parse_stottr
from ottr_tarql.cli import load_library
from ottr_tarql.runner import expand, generate_instances, read_csv, run_query

QNS = "http://example.org/ottr/query/"


def rows_for(case):
    csv_name, header = CASES[case]
    return read_csv((FIX / csv_name).read_text(encoding="utf8"), header=header)


@pytest.fixture(scope="module")
def decomposed(tmp_path_factory):
    out = tmp_path_factory.mktemp("lib")
    queries = [parse_query((FIX / c).read_text(encoding="utf8"), Path(c).stem) for c in CASES]
    decompose(queries).write(out)
    return out, load_library([str(out)])


def assert_same(expected: rdflib.Graph, actual: rdflib.Graph):
    if not isomorphic(expected, actual):
        _, only_e, only_a = graph_diff(to_isomorphic(expected), to_isomorphic(actual))
        pytest.fail(
            "graphs differ\nonly expected:\n"
            + only_e.serialize(format="nt")
            + "\nonly actual:\n"
            + only_a.serialize(format="nt")
        )


@pytest.mark.parametrize("case", list(CASES))
def test_tarql_roundtrip(case, decomposed):
    _, lib = decomposed
    original = parse_query((FIX / case).read_text(encoding="utf8"), Path(case).stem)
    rows = rows_for(case)
    expected = run_query(original, rows)
    assert len(expected) > 0
    composed = compose(lib, QNS + Path(case).stem).query
    assert_same(expected, run_query(composed, rows))


@pytest.mark.parametrize("case", list(CASES))
def test_templates_expand_like_tarql(case, decomposed):
    _, lib = decomposed
    original = parse_query((FIX / case).read_text(encoding="utf8"), Path(case).stem)
    rows = rows_for(case)
    insts = generate_instances(lib, QNS + Path(case).stem, rows)
    assert_same(run_query(original, rows), expand(lib, insts))


@needs_lutra
@pytest.mark.parametrize("case", list(CASES))
def test_lutra_matches_tarql(case, decomposed):
    libdir, lib = decomposed
    original = parse_query((FIX / case).read_text(encoding="utf8"), Path(case).stem)
    rows = rows_for(case)
    insts = generate_instances(lib, QNS + Path(case).stem, rows)
    assert_same(run_query(original, rows), lutra_expand(libdir, lib.prefixes, insts))


# ---------------------------------------------------------------------------- hand-written OTTR

PRODUCTS = FIX / "extra" / "products.stottr"
EX = "http://example.com/ns#"


@pytest.fixture(scope="module")
def products():
    lib = Library([parse_stottr(PRODUCTS.read_text(encoding="utf8"))])
    rows = read_csv((FIX / "extra" / "products.csv").read_text(encoding="utf8"))
    res = compose(lib, "t:Product")
    return lib, rows, res, run_query(res.query, rows)


def test_composed_query_semantics(products):
    _, _, _, g = products
    E = rdflib.Namespace(EX)
    RDFS = rdflib.RDFS
    # row 1: everything present
    assert (E.p1, RDFS.label, rdflib.Literal("Widget")) in g
    assert (E.p1, rdflib.RDF.type, E.Tools) in g
    assert (E.p1, E.price, rdflib.Literal("9.99", datatype=rdflib.XSD.decimal)) in g
    assert (E.p1, E.madeBy, E.acme) in g
    # row 2: defaults applied, mandatory ?maker missing -> whole t:Maker instance dropped
    assert (E.p2, RDFS.label, rdflib.Literal("unnamed")) in g
    assert (E.p2, rdflib.RDF.type, E.Misc) in g
    assert not list(g.triples((None, E.about, E.p2)))
    assert list(g.triples((None, E.itemOffered, E.p2)))  # t:Offer has optional ?price
    # row 3: mandatory ?product missing -> nothing at all from that row
    assert not list(g.triples((None, RDFS.label, rdflib.Literal("Ghost"))))
    assert len(list(g.triples((None, E.note, None)))) == 2  # only p1 and p3 have makers
    # row 4: full IRIs are accepted as well as prefixed names
    assert (E.p3, E.madeBy, E.globex) in g
    # cross | over a constant list, and an RDF list object
    assert (E.p1, rdflib.RDF.type, E.Shippable) in g
    lst = g.value(E.p1, E.tags)
    assert [str(x) for x in rdflib.collection.Collection(g, lst)] == ["new", "featured"]


def test_composed_matches_python_expander(products):
    lib, rows, _, g = products
    assert_same(g, expand(lib, generate_instances(lib, "t:Product", rows)))


@needs_lutra
def test_composed_matches_lutra(products):
    lib, rows, _, g = products
    insts = generate_instances(lib, "t:Product", rows)
    assert_same(g, lutra_expand(PRODUCTS.parent, lib.prefixes, insts))

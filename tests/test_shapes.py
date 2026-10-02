"""SHACL shapes generated from templates: valid output conforms, bad output does not.

Validation uses SHACL_Engine (``pip install shacl``); the tests skip without it.
"""

import subprocess
from pathlib import Path

import pytest
import rdflib

from conftest import CASES, EXAMPLE, FIX, LUTRA, lutra_available
from ottr_tarql import Library, compose, decompose, generate_shapes, parse_query, parse_stottr
from ottr_tarql.cli import load_library, main
from ottr_tarql.ottr import Instance
from ottr_tarql.runner import expand, normalize, read_csv, run_query
from ottr_tarql.terms import IRI, NONE, XSD, ListTerm, Literal

shacl = pytest.importorskip("shacl")

EX = "http://example.com/ns#"
SH = rdflib.Namespace("http://www.w3.org/ns/shacl#")


def validate(shapes: rdflib.Graph, data: rdflib.Graph):
    return shacl.Shapes.from_turtle(shapes.serialize(format="turtle")).validate_turtle(data.serialize(format="turtle"))


def problems(report) -> set:
    return {(r.focus_node, r.path, r.component) for r in report.results}


def assert_conforms(shapes, data):
    report = validate(shapes, data)
    assert report.conforms, "\n".join(f"{r.focus_node} {r.path} {r.value} {r.component}" for r in report.results)


def people_lib() -> Library:
    return Library([parse_stottr((EXAMPLE / "people.stottr").read_text(encoding="utf8"))])


def employees() -> list[Instance]:
    s = lambda x: Literal(x)  # noqa: E731
    return [
        Instance(EX + "Employee", [IRI(EX + "alice"), s("Alice"), s("alice@example.com"), IRI(EX + "carol"),
                                   ListTerm((s("python"), s("rust"), s("sparql")))]),
        Instance(EX + "Employee", [IRI(EX + "bob"), s("Bob"), NONE, IRI(EX + "carol"), ListTerm((s("sparql"),))]),
        Instance(EX + "Employee", [IRI(EX + "carol"), s("Carol"), s("carol@example.com"), NONE, NONE]),
    ]


# ---------------------------------------------------------------------------- no false positives


@pytest.fixture(scope="module")
def decomposed(tmp_path_factory):
    out = tmp_path_factory.mktemp("lib")
    queries = [parse_query((FIX / c).read_text(encoding="utf8"), Path(c).stem) for c in CASES]
    decompose(queries).write(out)
    lib = load_library([str(out)])
    shapes = generate_shapes(lib)
    assert len(set(shapes.subjects(rdflib.RDF.type, SH.NodeShape))) >= 10  # the checks below are not vacuous
    return lib, shapes


@pytest.mark.parametrize("case", list(CASES))
def test_decomposed_shapes_accept_the_query_output(case, decomposed):
    _, shapes = decomposed
    csv_name, header = CASES[case]
    rows = read_csv((FIX / csv_name).read_text(encoding="utf8"), header=header)
    assert_conforms(shapes, run_query(parse_query((FIX / case).read_text(encoding="utf8")), rows))


def test_example_output_conforms():
    lib = people_lib()
    assert_conforms(generate_shapes(lib, ["ex:Employee"]), expand(lib, employees()))


@pytest.mark.skipif(not lutra_available(), reason="java or tools/lutra.jar not available")
def test_lutra_bottr_output_conforms(tmp_path):
    out = tmp_path / "out.ttl"
    r = subprocess.run(
        ["java", "-jar", str(LUTRA), "-m", "expand", "-I", "bottr", "-l", str(EXAMPLE), "-L", "stottr",
         "-e", "stottr", "-O", "wottr", "-o", str(out), str(EXAMPLE / "employees.bottr.ttl")],
        capture_output=True, text=True, timeout=300,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert_conforms(generate_shapes(people_lib(), ["ex:Employee"]), normalize(rdflib.Graph().parse(out)))


def test_shapes_for_all_templates_accept_any_mix_of_them():
    """Without -T every template is a possible root: a person made by ex:Person
    alone (no ex:record) must then still conform."""
    lib = people_lib()
    dave = Instance(EX + "Person", [IRI(EX + "dave"), Literal("Dave"), NONE, NONE])
    data = expand(lib, employees() + [dave])
    assert_conforms(generate_shapes(lib), data)
    only_employee = validate(generate_shapes(lib, ["ex:Employee"]), data)
    assert (f"<{EX}dave>", f"<{EX}record>", "MinCountConstraintComponent") in problems(only_employee)


# ---------------------------------------------------------------------------- bad data is caught


def test_bad_values_from_a_csv_are_caught():
    lib = Library([parse_stottr((FIX / "extra" / "products.stottr").read_text(encoding="utf8"))])
    rows = read_csv((FIX / "extra" / "products.csv").read_text(encoding="utf8"))
    rows.append({"product": "ex:p9", "name": "Bad", "price": "abc", "category": "", "launched": "01/02/2024", "maker": ""})
    report = validate(generate_shapes(lib, ["t:Product"]), run_query(compose(lib, "t:Product").query, rows))
    found = {(f, p, c) for f, p, c in problems(report) if f == f"<{EX}p9>"}
    assert found == {
        (f"<{EX}p9>", f"<{EX}price>", "DatatypeConstraintComponent"),
        (f"<{EX}p9>", f"<{EX}launched>", "DatatypeConstraintComponent"),
    }


@pytest.mark.parametrize(
    "change, expected",
    [
        ("remove name", (f"<{EX}alice>", "<https://schema.org/name>", "MinCountConstraintComponent")),
        ("literal manager", (f"<{EX}alice>", f"<{EX}reportsTo>", "NodeKindConstraintComponent")),
        ("number as skill", (f"<{EX}bob>", f"<{EX}skill>", "DatatypeConstraintComponent")),
        ("drop source", (f"<{EX}alice>", f"<{EX}reporting>", "NodeConstraintComponent")),
    ],
)
def test_broken_example_data_is_caught(change, expected):
    lib = people_lib()
    g = expand(lib, employees())
    E = rdflib.Namespace(EX)
    if change == "remove name":
        g.remove((E.alice, rdflib.URIRef("https://schema.org/name"), None))
    elif change == "literal manager":
        g.add((E.alice, E.reportsTo, rdflib.Literal("carol")))
    elif change == "number as skill":
        g.add((E.bob, E.skill, rdflib.Literal(5)))
    else:
        g.remove((g.value(E.alice, E.reporting), E.source, None))
    assert expected in problems(validate(generate_shapes(lib, ["ex:Employee"]), g))


# ---------------------------------------------------------------------------- what the shapes say


def prop(shapes: rdflib.Graph, target, path):
    shape = next(shapes.subjects(SH.targetClass, rdflib.URIRef(target)))
    return next(p for p in shapes.objects(shape, SH.property) if shapes.value(p, SH.path) == rdflib.URIRef(path))


def test_mandatory_parameters_become_min_count():
    shapes = generate_shapes(people_lib(), ["ex:Employee"])
    person = "https://schema.org/Person"
    assert shapes.value(prop(shapes, person, "https://schema.org/name"), SH.minCount) == rdflib.Literal(1)
    assert shapes.value(prop(shapes, person, "https://schema.org/email"), SH.minCount) is None
    assert shapes.value(prop(shapes, person, EX + "record"), SH.minCount) == rdflib.Literal(1)
    assert shapes.value(prop(shapes, person, EX + "skill"), SH.minCount) is None  # list may be empty


SCORES = """@prefix ex: <http://example.com/ns#> . @prefix ottr: <http://ns.ottr.xyz/0.4/> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> . @prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
ex:Player [ ottr:IRI ?p, xsd:integer ?level, ? List<xsd:integer> ?scores ] :: {
  ottr:Triple(?p, rdf:type, ex:Player), ottr:Triple(?p, ex:level, ?level), ottr:Triple(?p, ex:scores, ?scores)
} ."""


def player(level, scores) -> rdflib.Graph:
    lib = Library([parse_stottr(SCORES)])
    return expand(lib, [Instance(EX + "Player", [IRI(EX + "p1"), level, ListTerm(tuple(scores))])])


def test_subtypes_are_opt_in():
    lib = Library([parse_stottr(SCORES)])
    data = player(Literal("5", XSD + "int"), [])
    exact = validate(generate_shapes(lib), data)
    assert (f"<{EX}p1>", f"<{EX}level>", "DatatypeConstraintComponent") in problems(exact)
    assert_conforms(generate_shapes(lib, allow_subtypes=True), data)


def test_list_members_are_checked():
    lib = Library([parse_stottr(SCORES)])
    one = Literal("1", XSD + "integer")
    assert_conforms(generate_shapes(lib), player(one, [one, Literal("2", XSD + "integer")]))
    report = validate(generate_shapes(lib), player(one, [one, Literal("two")]))
    assert not report.conforms


def test_max_counts_are_opt_in():
    lib = people_lib()
    g = expand(lib, employees())
    g.add((rdflib.URIRef(EX + "alice"), rdflib.URIRef("https://schema.org/name"), rdflib.Literal("Alicia")))
    assert_conforms(generate_shapes(lib, ["ex:Employee"]), g)
    strict = validate(generate_shapes(lib, ["ex:Employee"], max_counts=True), g)
    assert (f"<{EX}alice>", "<https://schema.org/name>", "MaxCountConstraintComponent") in problems(strict)


def test_cli(tmp_path):
    out = tmp_path / "shapes.ttl"
    assert main(["shapes", str(EXAMPLE / "people.stottr"), "-T", "ex:Employee", "-o", str(out)]) == 0
    g = rdflib.Graph().parse(out)
    assert (None, SH.targetClass, rdflib.URIRef("https://schema.org/Person")) in g

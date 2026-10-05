"""Checks against the real oxi-gen binary (skipped unless OXI_GEN or oxi_gen on PATH).

* the rdflib emulation agrees with oxi-gen on every fixture;
* queries composed from OTTR templates run on oxi-gen and give the same RDF as
  the original queries / as the template semantics.
"""

from pathlib import Path

import pytest

from conftest import CASES, EXAMPLE, FIX, assert_same, lutra_available, lutra_expand, needs_oxigen, oxigen
from ottr_tarql import Library, compose, decompose, parse_query, parse_stottr, serialize_query
from ottr_tarql.cli import load_library
from ottr_tarql.runner import generate_instances, read_csv, run_query
from test_roundtrip import QNS

pytestmark = needs_oxigen


@pytest.fixture(scope="module")
def decomposed(tmp_path_factory):
    out = tmp_path_factory.mktemp("lib")
    queries = [parse_query((FIX / c).read_text(encoding="utf8"), Path(c).stem) for c in CASES]
    decompose(queries).write(out)
    return load_library([str(out)])


@pytest.mark.parametrize("case", list(CASES))
def test_emulator_matches_oxigen(case, tmp_path):
    csv_name, header = CASES[case]
    text = (FIX / case).read_text(encoding="utf8")
    rows = read_csv((FIX / csv_name).read_text(encoding="utf8"), header=header)
    assert_same(oxigen(text, FIX / csv_name, header, tmp_path), run_query(parse_query(text), rows))


@pytest.mark.parametrize("case", list(CASES))
def test_roundtrip_on_oxigen(case, decomposed, tmp_path):
    csv_name, header = CASES[case]
    original = (FIX / case).read_text(encoding="utf8")
    composed = serialize_query(compose(decomposed, QNS + Path(case).stem).query)
    assert_same(oxigen(original, FIX / csv_name, header, tmp_path), oxigen(composed, FIX / csv_name, header, tmp_path))


def test_handwritten_template_on_oxigen(tmp_path):
    lib = Library([parse_stottr((FIX / "extra" / "products.stottr").read_text(encoding="utf8"))])
    query = compose(lib, "t:Product").query
    csv = FIX / "extra" / "products.csv"
    rows = read_csv(csv.read_text(encoding="utf8"))
    assert_same(run_query(query, rows), oxigen(serialize_query(query), csv, True, tmp_path))


def test_example_person_on_oxigen(tmp_path):
    """The guide's example: a template with mandatory parameters needs guards that
    work under oxi-gen's substitution of CSV values."""
    lib = Library([parse_stottr((EXAMPLE / "people.stottr").read_text(encoding="utf8"))])
    query = compose(lib, "ex:Person").query
    csv = EXAMPLE / "people.csv"
    rows = read_csv(csv.read_text(encoding="utf8"))
    actual = oxigen(serialize_query(query), csv, True, tmp_path)
    assert_same(run_query(query, rows), actual)
    if lutra_available():
        assert_same(lutra_expand(EXAMPLE, lib.prefixes, generate_instances(lib, "ex:Person", rows)), actual)


@pytest.mark.xfail(strict=True, reason="known emulator gap: rdflib's xsd:dateTime() accepts a bare date, oxi-gen's does not")
def test_emulator_datetime_cast_matches_oxigen(tmp_path):
    csv = tmp_path / "dates.csv"
    csv.write_text("d\n2019-03-01\n2019-03-01T09:30:00\n", encoding="utf8")
    text = (
        "PREFIX ex: <http://example.com/ns#>\nPREFIX xsd: <http://www.w3.org/2001/XMLSchema#>\n"
        "CONSTRUCT { ?s ex:when ?when } WHERE { BIND(IRI(CONCAT(str(ex:), ?d)) AS ?s) BIND(xsd:dateTime(?d) AS ?when) }\n"
    )
    rows = read_csv(csv.read_text(encoding="utf8"))
    assert_same(oxigen(text, csv, True, tmp_path), run_query(parse_query(text), rows), value=True)

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


def test_emulator_datetime_cast_matches_oxigen(tmp_path):
    """rdflib's own xsd:dateTime() would accept a bare date; oxi-gen's does not."""
    csv = tmp_path / "dates.csv"
    csv.write_text("d\n2019-03-01\n2019-03-01T09:30:00\n", encoding="utf8")
    text = (
        "PREFIX ex: <http://example.com/ns#>\nPREFIX xsd: <http://www.w3.org/2001/XMLSchema#>\n"
        "CONSTRUCT { ?s ex:when ?when } WHERE { BIND(IRI(CONCAT(str(ex:), ?d)) AS ?s) BIND(xsd:dateTime(?d) AS ?when) }\n"
    )
    rows = read_csv(csv.read_text(encoding="utf8"))
    assert_same(oxigen(text, csv, True, tmp_path), run_query(parse_query(text), rows))


# Cells that are, or are almost, numbers, booleans and date-times. Left out: values that
# crash oxi-gen (a year of 19 or more digits in an xsd:dateTime) and STRDT to derived
# integer types or xsd:dateTimeStamp, which oxi-gen retypes as xsd:integer / xsd:dateTime.
LITERAL_CELLS = [
    "007", "+7", "-0", "7.0", " 7 ", "5000.00", "0.50", "+1.50", "-0.0", ".5", "5.", "1e3", "1.5E3", "1.0e0",
    "-1.25E-2", "INF", "-INF", "NaN", "1", "0", "true", "false", "TRUE", "yes", "2019-03-01T09:30:00Z",
    "2019-03-01T09:30:00+00:00", "2019-03-01T09:30:00-00:00", "2019-03-01T09:30:00.250Z", "2019-03-01T09:30:00.0Z",
    "2019-03-01T09:30:00", "2019-03-01T24:00:00", "2019-03-01T09:30:00+01:00", "2019-02-30T00:00:00", "2019-03-01",
    "2019-03-01+00:00", "09:30:00.500Z", "0998", "-0044", "P1Y2M", "PT1.50S", "abc",
]
STRDT_TYPES = ["integer", "decimal", "double", "float", "boolean", "dateTime", "date", "time", "gYear", "duration", "string"]
CAST_TYPES = ["integer", "decimal", "double", "float", "boolean", "dateTime", "string"]


def test_emulator_literal_forms_match_oxigen(tmp_path):
    """STRDT and the casts give the same terms, lexical forms included, as oxi-gen."""
    csv = tmp_path / "cells.csv"
    csv.write_text("n,v\n" + "".join(f'{i},"{v}"\n' for i, v in enumerate(LITERAL_CELLS)), encoding="utf8")
    template = [f"?r ex:s_{t} ?s_{t} ." for t in STRDT_TYPES] + [f"?r ex:c_{t} ?c_{t} ." for t in CAST_TYPES]
    binds = [f"BIND(STRDT(?v, xsd:{t}) AS ?s_{t})" for t in STRDT_TYPES] + [f"BIND(xsd:{t}(?v) AS ?c_{t})" for t in CAST_TYPES]
    text = (
        "PREFIX ex: <http://example.com/ns#>\nPREFIX xsd: <http://www.w3.org/2001/XMLSchema#>\n"
        "CONSTRUCT {\n" + "\n".join(template) + "\n}\nWHERE {\n  BIND(IRI(CONCAT(str(ex:), ?n)) AS ?r)\n  "
        + "\n  ".join(binds) + "\n}\n"
    )
    rows = read_csv(csv.read_text(encoding="utf8"))
    assert_same(oxigen(text, csv, True, tmp_path), run_query(parse_query(text), rows))

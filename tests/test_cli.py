"""The command line: encodings and output formats."""

import os
import subprocess
import sys

import rdflib

from conftest import FIX, ROOT, assert_same, literal_forms_kept
from ottr_tarql import parse_query
from ottr_tarql.cli import main
from ottr_tarql.runner import normalize, read_csv, run_query

RETAIL = FIX / "retail"


def _cli(args: list[str], stdin: bytes | None = None) -> bytes:
    """Run the CLI in a process whose console encoding is cp1252, as on many Windows systems."""
    env = {**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONPATH": str(ROOT / "src")}
    env.pop("PYTHONUTF8", None)
    r = subprocess.run([sys.executable, "-m", "ottr_tarql.cli", *args], input=stdin, capture_output=True, env=env, timeout=120)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    return r.stdout


def test_stdout_is_utf8_without_crlf_whatever_the_console_encoding():
    """cp1252 has no "🚀", and "\\r\\n" in a Turtle long string would change its value."""
    query, csv = RETAIL / "tarql" / "customers.rq", RETAIL / "customers.csv"
    out = _cli(["run", "-q", str(query), "-i", str(csv)])
    with literal_forms_kept():
        written = normalize(rdflib.Graph().parse(data=out.decode("utf-8"), format="turtle"))
    rows = read_csv(csv.read_text(encoding="utf8"))
    assert_same(run_query(parse_query(query.read_text(encoding="utf8")), rows), written)


def test_stdin_is_read_as_utf8_whatever_the_console_encoding(tmp_path):
    """Read as cp1252, "Åsa 🚀" would be "Ã…sa ðŸš€", 9 characters instead of 5."""
    query = tmp_path / "strlen.rq"
    query.write_text("PREFIX ex: <http://example.com/ns#>\n"
                     "CONSTRUCT { ex:cell ex:length ?n } WHERE { BIND(STRLEN(?name) AS ?n) }\n", encoding="utf8")
    out = _cli(["run", "-q", str(query), "--ntriples"], stdin="name\nÅsa \U0001f680\n".encode("utf-8"))
    assert '"5"^^<http://www.w3.org/2001/XMLSchema#integer>' in out.decode("utf-8")


def test_turtle_output_keeps_lexical_forms(tmp_path):
    """rdflib's own Turtle writer would turn "8990"^^xsd:decimal into 8990.0."""
    query = RETAIL / "tarql" / "products.rq"
    csv = RETAIL / "products.csv"
    out = tmp_path / "products.ttl"
    assert main(["run", "-q", str(query), "-i", str(csv), "-o", str(out)]) == 0
    text = out.read_text(encoding="utf8")
    assert '"8990"^^xsd:decimal' in text and "129.9 " in text
    with literal_forms_kept():
        written = normalize(rdflib.Graph().parse(out, format="turtle"))
    rows = read_csv(csv.read_text(encoding="utf8"))
    assert_same(run_query(parse_query(query.read_text(encoding="utf8")), rows), written)

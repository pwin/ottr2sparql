import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager, nullcontext
from decimal import Decimal
from pathlib import Path

import pytest
import rdflib
from rdflib.compare import graph_diff, isomorphic, to_isomorphic

from ottr_tarql.ottr import render_document
from ottr_tarql.runner import normalize

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).resolve().parent / "fixtures"
LUTRA = ROOT / "tools" / "lutra.jar"
EXAMPLE = ROOT / "examples" / "oxigen"
OXI_GEN = os.environ.get("OXI_GEN") or shutil.which("oxi_gen")

# TARQL fixtures with the CSV used to exercise them
CASES = {
    "oxigen/escaped_chars.rq": ("oxigen/escaped_chars.csv", True),
    "oxigen/optional_field.rq": ("oxigen/optional_field.csv", True),
    "oxigen/quoted_empty.rq": ("oxigen/quoted_empty.csv", True),
    "oxigen/successor_field.rq": ("oxigen/successor_field.csv", True),
    "oxigen/with_dup.rq": ("oxigen/data_100.csv", True),
    "oxigen/splitfuncs.rq": ("oxigen/split.csv", False),
    "extra/people.rq": ("extra/people.csv", True),
    "extra/orgs.rq": ("extra/orgs.csv", True),
    "extra/constants.rq": ("extra/constants.csv", True),
    "extra/bound.rq": ("extra/bound.csv", True),
}


def lutra_available() -> bool:
    return LUTRA.exists() and shutil.which("java") is not None


def lutra_expand(library_dir: Path, prefixes, instances) -> rdflib.Graph:
    """Expand instances with the reference implementation."""
    with tempfile.TemporaryDirectory() as tmp:
        inst = Path(tmp) / "instances.stottr"
        out = Path(tmp) / "out.ttl"
        inst.write_text(render_document([], prefixes, instances), encoding="utf8")
        r = subprocess.run(
            ["java", "-jar", str(LUTRA), "-m", "expand", "-l", str(library_dir), "-L", "stottr", "-e", "stottr",
             "-I", "stottr", "-O", "wottr", "-o", str(out), "--haltOn", "ERROR", str(inst)],
            capture_output=True, text=True, timeout=300,
        )
        assert r.returncode == 0 and out.exists(), r.stdout + r.stderr
        return normalize(rdflib.Graph().parse(out, format="turtle"))


def lutra_lint(library_dir: Path) -> str:
    """Lint a template library with the reference implementation; return its report."""
    r = subprocess.run(
        ["java", "-jar", str(LUTRA), "-m", "lint", "-l", str(library_dir), "-L", "stottr", "-e", "stottr"],
        capture_output=True, text=True, timeout=300,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout + r.stderr


@contextmanager
def literal_forms_kept():
    """Build literals with their lexical form as written. By default rdflib rewrites
    date-times ("...Z" to "...+00:00"), booleans ("1" to "true") and integers ("007" to
    "7") into its own canonical forms, although it leaves decimals alone."""
    saved = rdflib.NORMALIZE_LITERALS
    rdflib.NORMALIZE_LITERALS = False
    try:
        yield
    finally:
        rdflib.NORMALIZE_LITERALS = saved


def oxigen(query_text: str, csv: Path, header: bool, tmp: Path, exact: bool = False) -> rdflib.Graph:
    """Run a query over a CSV file with the real oxi-gen binary. With ``exact``, the
    literals keep oxi-gen's lexical forms."""
    q, out = tmp / "q.rq", tmp / "out.nt"
    q.write_text(query_text, encoding="utf8")
    args = [OXI_GEN, "-q", str(q), "-i", str(csv), "-o", str(out), "--ntriples"]
    if not header:
        args.append("-H")
    r = subprocess.run(args, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    with literal_forms_kept() if exact else nullcontext():
        return normalize(rdflib.Graph().parse(out, format="nt"))


def by_value(g: rdflib.Graph) -> rdflib.Graph:
    """Give every well-formed typed literal one lexical form per value, so that
    "129.90"^^xsd:decimal (kept by Lutra and rdflib) matches "129.9"^^xsd:decimal
    (oxi-gen's canonical form), and "...Z" matches "...+00:00". Ill-typed literals
    are left alone."""
    out = rdflib.Graph()
    for s, p, o in g:
        if isinstance(o, rdflib.Literal) and o.datatype is not None and not o.ill_typed:
            v = o.toPython()
            if isinstance(v, bool):
                lex = "true" if v else "false"
            elif isinstance(v, Decimal):
                lex = format(v.normalize(), "f")
            elif isinstance(v, (int, float)):
                lex = repr(v)
            elif hasattr(v, "isoformat"):
                lex = v.isoformat()
            else:
                lex = str(o)
            o = rdflib.Literal(lex, datatype=o.datatype)
        out.add((s, p, o))
    return out


def assert_same(expected: rdflib.Graph, actual: rdflib.Graph, value: bool = False):
    """Graph isomorphism; with ``value``, typed literals are compared by value."""
    if value:
        expected, actual = by_value(expected), by_value(actual)
    if not isomorphic(expected, actual):
        _, only_e, only_a = graph_diff(to_isomorphic(expected), to_isomorphic(actual))
        pytest.fail(
            "graphs differ\nonly expected:\n"
            + only_e.serialize(format="nt")
            + "\nonly actual:\n"
            + only_a.serialize(format="nt")
        )


needs_lutra = pytest.mark.skipif(not lutra_available(), reason="java or tools/lutra.jar not available")
needs_oxigen = pytest.mark.skipif(not OXI_GEN, reason="set OXI_GEN to an oxi-gen binary to run these tests")

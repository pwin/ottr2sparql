import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import rdflib

from ottr_tarql.ottr import render_document
from ottr_tarql.runner import normalize

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).resolve().parent / "fixtures"
LUTRA = ROOT / "tools" / "lutra.jar"

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


needs_lutra = pytest.mark.skipif(not lutra_available(), reason="java or tools/lutra.jar not available")

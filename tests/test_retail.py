"""Native and converted mappings for three complex CSV files (tests/fixtures/retail).

The same CSV -> RDF transformation is written twice, by hand:

* ``tarql/*.rq``: TARQL queries for oxi-gen (the "native TARQL");
* ``ottr/*.stottr``: a modular OTTR library (the "native OTTR"). Generic templates take
  untyped ("regular") values, domain templates are strongly typed, and one root template
  per CSV has the CSV columns as its parameters.

Each is converted into the other form:

* ``converted/tarql-from-ottr/``: compose(native OTTR);
* ``converted/ottr-from-tarql/``: decompose(native TARQL);
* ``converted/ottr-round-trip/``: decompose(compose(native OTTR)).

Every route must produce the RDF in ``expected/*.nt``, which is oxi-gen's output for the
native TARQL, term for term and lexical forms included. That holds for Lutra and the rdflib
emulator too, because ``instances`` and ``run`` write typed values the way oxi-gen does
("129.90" as "129.9"^^xsd:decimal; see ``ottr_tarql.literals``).

``converted/METRICS.md`` compares the native and converted forms, and docs/COMPARISON.md
discusses the results. Regenerate the checked-in files under ``converted/`` and
``expected/`` with ``UPDATE_RETAIL=1`` (``expected/`` also needs ``OXI_GEN``).
"""

import os
import re
import subprocess
import tempfile
from pathlib import Path

import pytest
import rdflib
from rdflib.compare import to_canonical_graph

from conftest import (
    FIX,
    LUTRA,
    OXI_GEN,
    assert_same,
    literal_forms_kept,
    lutra_available,
    lutra_expand,
    lutra_lint,
    needs_lutra,
    needs_oxigen,
    oxigen,
)
from ottr_tarql import compose, decompose, generate_shapes, parse_query, serialize_query
from ottr_tarql.cli import load_library
from ottr_tarql.ottr import IRI_TYPES, Library, render_document, render_type
from ottr_tarql.runner import expand, generate_instances, normalize, read_csv, run_query
from ottr_tarql.sparql import Bind
from ottr_tarql.terms import XSD, Var

RETAIL = FIX / "retail"
CONVERTED = RETAIL / "converted"
DATASETS = {"customers": "rt:CustomerRow", "products": "rt:ProductRow", "orders": "rt:OrderRow"}
TEMPLATE_NS = "http://example.com/retail/tpl/"
QUERY_NS = "http://example.com/retail/q/"
UPDATE = os.environ.get("UPDATE_RETAIL") == "1"

EX = rdflib.Namespace("http://example.com/ns#")
SCHEMA = rdflib.Namespace("https://schema.org/")
CUST = rdflib.Namespace("http://example.com/customer/")
PROD = rdflib.Namespace("http://example.com/product/")
ORD = rdflib.Namespace("http://example.com/order/")
CUR = rdflib.Namespace("http://example.com/currency/")


def csv_path(d: str) -> Path:
    return RETAIL / f"{d}.csv"


def rows(d: str) -> list[dict]:
    return read_csv(csv_path(d).read_text(encoding="utf8"))


def native_text(d: str) -> str:
    return (RETAIL / "tarql" / f"{d}.rq").read_text(encoding="utf8")


def expected(d: str) -> rdflib.Graph:
    with literal_forms_kept():
        return normalize(rdflib.Graph().parse(RETAIL / "expected" / f"{d}.nt", format="nt"))


def snapshot(path: Path, text: str) -> None:
    """Check a generated file against its checked-in copy, or rewrite it with UPDATE_RETAIL=1."""
    if UPDATE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf8", newline="\n")
    assert path.exists() and path.read_text(encoding="utf8") == text, f"{path} is out of date: rerun with UPDATE_RETAIL=1"


def decomposed_files(directory: Path) -> dict[str, str]:
    return {f.relative_to(directory).as_posix(): f.read_text(encoding="utf8") for f in sorted(directory.rglob("*.stottr"))}


# ---------------------------------------------------------------------------- the four mappings


@pytest.fixture(scope="module")
def native_ottr() -> Library:
    return load_library([str(RETAIL / "ottr")])


@pytest.fixture(scope="module")
def composed(native_ottr) -> dict[str, str]:
    """OTTR -> TARQL: one query per root template."""
    return {d: serialize_query(compose(native_ottr, root).query) for d, root in DATASETS.items()}


def _decompose(texts: dict[str, str], out: Path) -> tuple[Path, Library]:
    # sorted, as `ottr-tarql decompose tarql/*.rq` would read them
    decompose([parse_query(texts[d], d) for d in sorted(texts)], TEMPLATE_NS, QUERY_NS).write(out)
    return out, load_library([str(out)])


@pytest.fixture(scope="module")
def decomposed(tmp_path_factory) -> tuple[Path, Library]:
    """TARQL -> OTTR: one shared library for the three native queries."""
    return _decompose({d: native_text(d) for d in DATASETS}, tmp_path_factory.mktemp("from-tarql"))


@pytest.fixture(scope="module")
def ottr_round_trip(composed, tmp_path_factory) -> tuple[Path, Library]:
    """OTTR -> TARQL -> OTTR."""
    return _decompose(composed, tmp_path_factory.mktemp("round-trip"))


# ---------------------------------------------------------------------------- the reference output


@needs_oxigen
@pytest.mark.parametrize("d", DATASETS)
def test_native_tarql_on_oxigen(d, tmp_path):
    """oxi-gen runs the hand-written TARQL; its output is the reference for every other route."""
    out = oxigen(native_text(d), csv_path(d), True, tmp_path)
    if UPDATE:
        # N-Triples keep oxi-gen's lexical forms (rdflib's Turtle writer turns "250000"^^xsd:decimal
        # into 250000.0); canonical blank node labels and sorted lines keep the file stable
        lines = to_canonical_graph(out).serialize(format="nt").splitlines()
        snapshot(RETAIL / "expected" / f"{d}.nt", "\n".join(sorted(ln for ln in lines if ln)) + "\n")
    assert_same(expected(d), out)


def test_reference_output_has_the_tricky_cells_right():
    """What the CSVs exercise, checked on the reference output (and so on every route)."""
    c, p, o = expected("customers"), expected("products"), expected("orders")
    lit = rdflib.Literal

    # quoting: embedded newlines and commas, "" and \" quotes, \& escapes, tabs, padding, Unicode
    assert c.value(CUST.C001, rdflib.RDFS.comment) == lit('Key account.\nInvoices by e-mail; "net 30" terms.')
    assert c.value(CUST.C003, rdflib.RDFS.comment) == lit('Works for "Globex Nordic" — Bergen office')
    assert c.value(CUST.C005, SCHEMA.legalName) == lit("Smith, Jones & Partners LLP")
    assert c.value(CUST.C006, rdflib.RDFS.comment) == lit("Tab\tseparated; emoji 🚀")
    assert c.value(CUST.C006, SCHEMA.name) == lit("  Initech  ")
    assert c.value(CUST.C003, SCHEMA.name) == lit("Åsa Ødegård")
    assert o.value(ORD["O-1001"], EX.giftMessage) == lit('Til mor: "Gratulerer!"\nKlem fra Åsa')
    # cells holding only spaces or a tab are empty: no comment, and no address without a locality
    assert c.value(CUST.C004, rdflib.RDFS.comment) is None
    assert c.value(CUST.C005, rdflib.RDFS.comment) is None
    assert c.value(CUST.C006, SCHEMA.address) is None
    # a column read as a plain string keeps its leading zero
    addr = c.value(CUST.C002, SCHEMA.address)
    assert c.value(addr, SCHEMA.postalCode) == lit("0155")
    # ids given as full IRIs or as prefixed names
    assert (CUST.C005, rdflib.RDF.type, EX.Partnership) in c
    assert (PROD["P-301"], rdflib.RDF.type, SCHEMA.Product) in p
    assert (ORD["O-1003"], SCHEMA.customer, CUST.C005) in o
    # optional nodes exist only with their defining value; the currency defaults to EUR
    assert p.value(PROD["P-300"], SCHEMA.priceSpecification) is None
    assert p.value(PROD["P-300"], SCHEMA.weight) is None
    assert p.value(p.value(PROD["P-101"], SCHEMA.priceSpecification), SCHEMA.priceCurrency) == CUR.EUR
    assert c.value(CUST.C004, SCHEMA.address) is None  # country but no locality
    assert set(p.subjects(rdflib.RDF.type, EX.DiscontinuedProduct)) == {PROD["P-099"]}
    assert (PROD["P-099"], EX.replacedBy, PROD["P-101"]) in p
    assert (CUST.C001, SCHEMA.subOrganization, CUST.C002) in c
    # one line node per row with a line number; shipments and unit prices hang off lines
    items = {order: len(set(o.objects(order, SCHEMA.orderedItem))) for order in o.subjects(rdflib.RDF.type, SCHEMA.Order)}
    assert items == {ORD["O-1001"]: 2, ORD["O-1002"]: 3, ORD["O-1003"]: 0, ORD["O-1004"]: 2}
    assert len(set(o.subjects(rdflib.RDF.type, EX.Shipment))) == 4
    assert len(set(o.subjects(rdflib.RDF.type, SCHEMA.PriceSpecification))) == 6
    assert sorted(int(r) for r in o.objects(None, EX.sourceRow)) == [0, 1, 2, 3, 4, 6, 7]
    # language-tagged constants
    assert lit("kundekonto", lang="nb") in set(c.objects(None, rdflib.RDFS.label))


@pytest.mark.parametrize("d", DATASETS)
def test_native_tarql_on_the_emulator(d):
    assert_same(expected(d), run_query(parse_query(native_text(d)), rows(d)))


# ---------------------------------------------------------------------------- native OTTR


@needs_lutra
def test_native_ottr_lints_clean():
    report = lutra_lint(RETAIL / "ottr")
    assert "WARNING" not in report and "ERROR" not in report, report


@needs_lutra
@pytest.mark.parametrize("d", DATASETS)
def test_native_ottr_on_lutra(d, native_ottr):
    insts = generate_instances(native_ottr, DATASETS[d], rows(d))
    assert_same(expected(d), lutra_expand(RETAIL / "ottr", native_ottr.prefixes, insts))


@pytest.mark.parametrize("d", DATASETS)
def test_native_ottr_on_the_python_expander(d, native_ottr):
    insts = generate_instances(native_ottr, DATASETS[d], rows(d))
    assert_same(expected(d), expand(native_ottr, insts))


@pytest.mark.parametrize("d", DATASETS)
def test_root_parameters_are_the_csv_columns(d, native_ottr):
    header = list(rows(d)[0])
    assert [c for c in compose(native_ottr, DATASETS[d]).columns if c != "ROWNUM"] == header


# ---------------------------------------------------------------------------- OTTR -> TARQL


@pytest.mark.parametrize("d", DATASETS)
def test_composed_tarql_is_checked_in(d, composed):
    snapshot(CONVERTED / "tarql-from-ottr" / f"{d}.rq", composed[d])


@needs_oxigen
@pytest.mark.parametrize("d", DATASETS)
def test_composed_tarql_on_oxigen(d, composed, tmp_path):
    assert_same(expected(d), oxigen(composed[d], csv_path(d), True, tmp_path))


@pytest.mark.parametrize("d", DATASETS)
def test_composed_tarql_on_the_emulator(d, composed):
    assert_same(expected(d), run_query(parse_query(composed[d]), rows(d)))


# ---------------------------------------------------------------------------- TARQL -> OTTR


def test_decomposed_ottr_is_checked_in(decomposed):
    files = decomposed_files(decomposed[0])
    assert sorted(files) == ["queries/customers.stottr", "queries/orders.stottr", "queries/products.stottr",
                             "templates.stottr", "tq-vocabulary.stottr"]
    for name, text in files.items():
        snapshot(CONVERTED / "ottr-from-tarql" / name, text)


@needs_lutra
def test_decomposed_ottr_lints_clean(decomposed):
    report = lutra_lint(decomposed[0])
    assert "WARNING" not in report and "ERROR" not in report, report


@needs_lutra
@pytest.mark.parametrize("d", DATASETS)
def test_decomposed_ottr_on_lutra(d, decomposed):
    path, lib = decomposed
    insts = generate_instances(lib, QUERY_NS + d, rows(d))
    assert_same(expected(d), lutra_expand(path, lib.prefixes, insts))


@pytest.mark.parametrize("d", DATASETS)
def test_decomposed_ottr_on_the_python_expander(d, decomposed):
    _, lib = decomposed
    assert_same(expected(d), expand(lib, generate_instances(lib, QUERY_NS + d, rows(d))))


# ---------------------------------------------------------------------------- round trips


@pytest.mark.parametrize("d", DATASETS)
def test_tarql_round_trip_gives_back_the_query(d, decomposed):
    """TARQL -> OTTR -> TARQL is lossless apart from comments and layout."""
    original = parse_query(native_text(d))
    back = compose(decomposed[1], QUERY_NS + d).query
    assert back.prefixes.map == original.prefixes.map
    assert set(back.triples) == set(original.triples)
    assert back.where == original.where


@needs_oxigen
@pytest.mark.parametrize("d", DATASETS)
def test_tarql_round_trip_on_oxigen(d, decomposed, tmp_path):
    back = serialize_query(compose(decomposed[1], QUERY_NS + d).query)
    assert_same(expected(d), oxigen(back, csv_path(d), True, tmp_path))


@needs_lutra
def test_ottr_round_trip_lints_clean(ottr_round_trip):
    report = lutra_lint(ottr_round_trip[0])
    assert "WARNING" not in report and "ERROR" not in report, report


def test_ottr_round_trip_is_checked_in(ottr_round_trip):
    for name, text in decomposed_files(ottr_round_trip[0]).items():
        snapshot(CONVERTED / "ottr-round-trip" / name, text)


@pytest.mark.parametrize("d", DATASETS)
def test_ottr_round_trip_expands_the_same(d, ottr_round_trip):
    path, lib = ottr_round_trip
    insts = generate_instances(lib, QUERY_NS + d, rows(d))
    g = lutra_expand(path, lib.prefixes, insts) if lutra_available() else expand(lib, insts)
    assert_same(expected(d), g)


# ---------------------------------------------------------------------------- comparing the forms


def query_metrics(text: str) -> dict[str, int]:
    q = parse_query(text)
    binds = [c for c in q.where if isinstance(c, Bind)]
    guards = [b for b in binds if re.fullmatch(r"_g\d+", b.var)]
    defaults = [b for b in binds if re.fullmatch(r".+_d\d+", b.var) and b.expr.startswith("COALESCE(")]
    return {
        "CONSTRUCT triple patterns": len(q.triples),
        "  with a variable predicate": sum(isinstance(p, Var) for _, p, _ in q.triples),
        "BINDs": len(binds),
        "  guards (`?_gN`)": len(guards),
        "  defaults (`COALESCE`)": len(defaults),
        "lines (serialised, no comments)": len(serialize_query(q).splitlines()),
    }


def _type_kind(t) -> str:
    if t is None:
        return "untyped"
    if t in IRI_TYPES:
        return "IRI"
    if t == XSD + "string":
        return "string"
    return "datatype"


def library_metrics(lib: Library, roots: list[str]) -> dict[str, int]:
    roots = [lib.resolve(r) for r in roots]
    own = {iri: t for iri, t in lib.templates.items() if t.kind == "template" and iri not in roots}

    def reach(iri: str, seen: set) -> set:
        for inst in lib.get(iri).body or []:
            if inst.template in own and inst.template not in seen:
                seen.add(inst.template)
                reach(inst.template, seen)
        return seen

    def depth(iri: str) -> int:
        t = lib.get(iri)
        return 0 if t.kind != "template" else 1 + max((depth(i.template) for i in t.body), default=0)

    users: dict[str, int] = {iri: 0 for iri in own}
    for r in roots:
        for iri in reach(r, set()):
            users[iri] += 1
    params = [p for t in own.values() for p in t.params]
    kinds = [_type_kind(p.type) for p in params]
    return {
        "templates (not counting roots)": len(own),
        "  used by more than one root": sum(n > 1 for n in users.values()),
        "instances in template bodies": sum(len(t.body) for t in own.values()),
        "nesting depth (root to `ottr:Triple`)": max(depth(r) for r in roots),
        "parameters": len(params),
        "  datatype (`xsd:decimal`, …)": kinds.count("datatype"),
        "  IRI (`ottr:IRI`, `owl:Class`, …)": kinds.count("IRI"),
        "  `xsd:string`": kinds.count("string"),
        "  untyped": kinds.count("untyped"),
        "  mandatory": sum(not p.optional for p in params),
        "  non-blank (`!`)": sum(p.nonblank for p in params),
        "  with a default": sum(p.default is not None for p in params),
    }


def column_types(native_ottr: Library, from_tarql: Library) -> list[tuple[str, str, str, str]]:
    """(dataset, column, type in the native root, type of the value decompose made from it)."""
    out = []
    for d, root in DATASETS.items():
        binds = parse_query(native_text(d)).bind_targets()
        droot = from_tarql.get(QUERY_NS + d)
        for p in native_ottr.get(native_ottr.resolve(root)).params:
            col = re.compile(r"\?" + re.escape(p.name) + r"\b")
            carriers = [
                q for q in droot.params
                if (q.name == p.name and q.name not in binds) or (q.name in binds and col.search(binds[q.name]) and "BNODE" not in binds[q.name])
            ]
            assert len(carriers) == 1, (d, p.name, [q.name for q in carriers])
            show = lambda lib, t: "untyped" if t is None else render_type(t, lib.prefixes)  # noqa: E731
            out.append((d, p.name, show(native_ottr, p.type), show(from_tarql, carriers[0].type)))
    return out


def classify(native: str, converted: str) -> str:
    if native == converted:
        return "same"
    if converted == "untyped":
        return "lost"
    if native == "untyped":
        return "added"
    return "generalised"


def test_native_ottr_is_modular_and_converted_ottr_is_flat(native_ottr, decomposed, ottr_round_trip):
    roots = [QUERY_NS + d for d in DATASETS]
    native = library_metrics(native_ottr, list(DATASETS.values()))
    from_tarql = library_metrics(decomposed[1], roots)
    round_trip = library_metrics(ottr_round_trip[1], roots)
    # the hand-written library nests templates and shares them between the CSVs
    assert native["nesting depth (root to `ottr:Triple`)"] == 4
    assert native["  used by more than one root"] == 4  # rt:Value, rt:Typed, rt:Inverse, rt:Price
    # decompose makes one flat template per subject node; only the price node is shared
    assert from_tarql["nesting depth (root to `ottr:Triple`)"] == 2
    assert from_tarql["  used by more than one root"] == 1
    # decompose leaves value parameters optional: nodes are made in the WHERE clause instead
    assert from_tarql["  mandatory"] == from_tarql["templates (not counting roots)"]
    assert native["  mandatory"] > native["templates (not counting roots)"]
    # after OTTR -> TARQL -> OTTR, compose's guards come back as nesting and mandatory
    # parameters, with constant predicates
    assert round_trip["nesting depth (root to `ottr:Triple`)"] == 3
    assert round_trip["  mandatory"] > from_tarql["  mandatory"]
    assert round_trip["  non-blank (`!`)"] == round_trip["  untyped"] == 0


def test_composed_tarql_is_larger_but_has_the_same_patterns(composed):
    for d in DATASETS:
        native, conv = query_metrics(native_text(d)), query_metrics(composed[d])
        assert conv["CONSTRUCT triple patterns"] == native["CONSTRUCT triple patterns"], d
        assert native["  guards (`?_gN`)"] == 0 < conv["  guards (`?_gN`)"], d
        assert conv["BINDs"] > native["BINDs"], d


def test_decompose_recovers_most_column_types(native_ottr, decomposed):
    found: dict[str, set] = {}
    for d, col, native, conv in column_types(native_ottr, decomposed[1]):
        found.setdefault(classify(native, conv), set()).add(f"{d}.{col}")
    # COALESCE(...) and IF(...) take the type their branches agree on, so no type is lost
    assert "lost" not in found
    # a column used as it is becomes xsd:string, and ?ROWNUM xsd:integer, where the native
    # templates leave them untyped
    assert found["added"] == {"customers.name", "customers.legal_name", "customers.email", "customers.notes",
                              "products.description", "orders.channel", "orders.gift_message", "orders.ROWNUM"}
    # TARQL has no way to say a value is a class or an individual
    assert found["generalised"] == {"customers.kind", "products.category", "orders.status"}
    assert len(found["same"]) == 37


def test_metrics_are_checked_in(native_ottr, composed, decomposed, ottr_round_trip):
    roots = [QUERY_NS + d for d in DATASETS]
    lines = [
        "# Native and converted mappings compared",
        "",
        "Generated by `tests/test_retail.py` (`UPDATE_RETAIL=1 pytest tests/test_retail.py`).",
        "See [docs/COMPARISON.md](../../../../docs/COMPARISON.md) for the discussion.",
        "",
        "## TARQL",
        "",
        "Native: [`tarql/`](../tarql). From OTTR: [`tarql-from-ottr/`](tarql-from-ottr).",
        "",
    ]
    for d in DATASETS:
        native, conv = query_metrics(native_text(d)), query_metrics(composed[d])
        lines += [f"### {d}", "", "| | native | from OTTR |", "|---|---:|---:|"]
        lines += [f"| {k} | {native[k]} | {conv[k]} |" for k in native] + [""]
    libs = {
        "native": library_metrics(native_ottr, list(DATASETS.values())),
        "from TARQL": library_metrics(decomposed[1], roots),
        "round trip": library_metrics(ottr_round_trip[1], roots),
    }
    lines += [
        "## OTTR",
        "",
        "Native: [`ottr/`](../ottr). From TARQL: [`ottr-from-tarql/`](ottr-from-tarql).",
        "Round trip (OTTR → TARQL → OTTR): [`ottr-round-trip/`](ottr-round-trip).",
        "",
        "| | " + " | ".join(libs) + " |",
        "|---|" + "---:|" * len(libs),
    ]
    lines += [f"| {k} | " + " | ".join(str(m[k]) for m in libs.values()) + " |" for k in libs["native"]]
    lines += [
        "",
        "## Column types",
        "",
        "The type of each CSV column in the native root template, and the type decompose gave the",
        "value made from that column.",
        "",
        "| CSV | column | native OTTR | from TARQL | |",
        "|---|---|---|---|---|",
    ]
    for d, col, native, conv in column_types(native_ottr, decomposed[1]):
        verdict = classify(native, conv)
        lines.append(f"| {d} | `{col}` | `{native}` | `{conv}` | {'' if verdict == 'same' else verdict} |")
    snapshot(CONVERTED / "METRICS.md", "\n".join(lines) + "\n")


# ---------------------------------------------------------------------------- typing and bad cells

# One or two bad cells per CSV. The native TARQL reads some columns with casts
# (xsd:decimal(...)), which drop a bad value, and dates with STRDT, which keeps it.
DIRTY = {
    "customers": [(",1,5000.50,", ",1,5k,"), ("1985-02-28", "28/02/1985")],
    "products": [("129.90,cur:USD", "N/A,cur:USD"), (",true,140,", ",true,lots,")],
    "orders": [(",prod:P-099,1,999.99,", ",prod:P-099,one,999.99,")],
}
BAD = {"customers": {"5k", "28/02/1985"}, "products": {"N/A", "lots"}, "orders": {"one"}}
KEPT_BY_STRDT = {"28/02/1985"}
FLAGGED = {
    "customers": {("<https://schema.org/birthDate>", "DatatypeConstraintComponent"),
                  ("<http://example.com/ns#creditLimit>", "DatatypeConstraintComponent")},
    "products": {("<https://schema.org/price>", "DatatypeConstraintComponent"),
                 ("<http://example.com/ns#stockLevel>", "DatatypeConstraintComponent")},
    "orders": {("<https://schema.org/orderQuantity>", "DatatypeConstraintComponent")},
}


def dirty_csv(d: str, tmp: Path) -> Path:
    text = csv_path(d).read_text(encoding="utf8")
    for old, new in DIRTY[d]:
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    out = tmp / f"{d}.csv"
    out.write_text(text, encoding="utf8", newline="\n")
    return out


def ill_typed(g: rdflib.Graph) -> set[str]:
    return {str(o) for o in g.objects() if isinstance(o, rdflib.Literal) and o.ill_typed}


def lutra_errors(library_dir: Path, prefixes, instances) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        inst = Path(tmp) / "instances.stottr"
        inst.write_text(render_document([], prefixes, instances), encoding="utf8")
        r = subprocess.run(
            ["java", "-jar", str(LUTRA), "-m", "expand", "-l", str(library_dir), "-L", "stottr", "-e", "stottr",
             "-I", "stottr", "-O", "wottr", "-o", str(Path(tmp) / "out.ttl"), "--haltOn", "ERROR", str(inst)],
            capture_output=True, text=True, timeout=300,
        )
        return r.stdout + r.stderr


@pytest.mark.parametrize("d", DATASETS)
def test_bad_cells(d, native_ottr, composed, tmp_path):
    """Casts drop a bad cell silently and STRDT keeps it as an ill-typed literal. The
    strongly typed templates refuse it in Lutra, and their SHACL shapes flag it."""
    shacl = pytest.importorskip("shacl")
    csv = dirty_csv(d, tmp_path)
    dirty_rows = read_csv(csv.read_text(encoding="utf8"))

    def run(text: str) -> rdflib.Graph:
        return oxigen(text, csv, True, tmp_path) if OXI_GEN else run_query(parse_query(text), dirty_rows)

    native, from_ottr = run(native_text(d)), run(composed[d])
    assert ill_typed(native) == BAD[d] & KEPT_BY_STRDT
    assert ill_typed(from_ottr) == BAD[d]
    if d == "products":  # a price that does not parse removes the whole price node from the native output
        assert native.value(PROD["P-100"], SCHEMA.priceSpecification) is None
        assert from_ottr.value(PROD["P-100"], SCHEMA.priceSpecification) is not None

    shapes = shacl.Shapes.from_turtle(generate_shapes(native_ottr, list(DATASETS.values())).serialize(format="turtle"))
    for g, flagged in ((native, {f for f in FLAGGED[d] if "birthDate" in f[0]}), (from_ottr, FLAGGED[d])):
        report = shapes.validate_turtle(g.serialize(format="turtle"))
        assert {(r.path, r.component) for r in report.results} == flagged

    if lutra_available():
        log = lutra_errors(RETAIL / "ottr", native_ottr.prefixes, generate_instances(native_ottr, DATASETS[d], dirty_rows))
        assert set(re.findall(r"The value '([^']*)' is not in the lexical space", log)) == BAD[d], log


@pytest.mark.parametrize("d", DATASETS)
def test_shapes_from_both_libraries_accept_the_reference_output(d, native_ottr, decomposed):
    shacl = pytest.importorskip("shacl")
    data = expected(d).serialize(format="turtle")
    for shapes in (generate_shapes(native_ottr, list(DATASETS.values())), generate_shapes(decomposed[1])):
        report = shacl.Shapes.from_turtle(shapes.serialize(format="turtle")).validate_turtle(data)
        assert report.conforms, [(r.focus_node, r.path, r.value, r.component) for r in report.results]

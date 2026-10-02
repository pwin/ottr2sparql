"""Command line: ottr-tarql {decompose,compose,shapes,instances,run,expand}."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .compose import compose, roots_in
from .decompose import decompose
from .ottr import Library, parse_stottr, render_document
from .runner import expand, generate_instances, read_csv, run_query
from .shapes import DEFAULT_SHAPES_NS, generate_shapes
from .sparql import Unsupported, parse_query, serialize_query


def load_library(paths: list[str]) -> Library:
    lib = Library()
    for p in paths:
        path = Path(p)
        files = sorted(path.rglob("*.stottr")) if path.is_dir() else [path]
        for f in files:
            try:
                lib.add(parse_stottr(f.read_text(encoding="utf8")))
            except ValueError as e:
                raise SystemExit(f"{f}: {e}") from None
    return lib


def _out(text: str, path: str | None) -> None:
    if path:
        Path(path).write_text(text, encoding="utf8")
    else:
        sys.stdout.write(text)


def _rows(args) -> list[dict]:
    text = Path(args.input).read_text(encoding="utf-8-sig") if args.input else sys.stdin.read()
    delim = "\t" if args.tab else args.delimiter
    return read_csv(text, delim, header=not args.no_header_row, quote=args.quote_char, escape=args.escape_char)


def _csv_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("-i", "--input", help="CSV file (default: stdin)")
    p.add_argument("-d", "--delimiter", default=",")
    p.add_argument("-t", "--tab", action="store_true", help="tab separated input")
    p.add_argument("-H", "--no-header-row", action="store_true", help="columns are named a-z, A-Z")
    p.add_argument("-p", "--escape-char", default="\\", help="escape character inside quoted fields (default: backslash)")
    p.add_argument("--quote-char", default='"')


def cmd_decompose(a) -> int:
    queries, status = [], 0
    for f in a.queries:
        path = Path(f)
        try:
            queries.append(parse_query(path.read_text(encoding="utf8"), path.stem))
        except Unsupported as e:
            print(f"skipped {path}: {e}", file=sys.stderr)
            status = 2
        except ValueError as e:
            raise SystemExit(f"{path}: {e}") from None
    d = decompose(queries, a.template_ns, a.query_ns, factor=not a.no_factor)
    for f in d.write(a.output):
        print(f)
    return status


def cmd_compose(a) -> int:
    lib = load_library(a.library)
    targets = roots_in(lib) if a.all else [a.template]
    if not targets or targets == [None]:
        raise SystemExit("give --template IRI or --all")
    for t in targets:
        try:
            res = compose(lib, t, infer_binds=not a.no_infer_binds)
        except (Unsupported, KeyError) as e:
            raise SystemExit(f"{t}: {e}") from None
        text = serialize_query(res.query)
        if a.all:
            out = Path(a.output or ".") / f"{res.query.name}.rq"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding="utf8")
            print(out)
        else:
            _out(text, a.output)
    return 0


def cmd_shapes(a) -> int:
    lib = load_library(a.library)
    g = generate_shapes(lib, a.template, a.shapes_ns, max_counts=a.max_counts, allow_subtypes=a.allow_subtypes)
    _out(g.serialize(format="turtle"), a.output)
    return 0


def cmd_instances(a) -> int:
    lib = load_library(a.library)
    insts = generate_instances(lib, a.template, _rows(a))
    _out(render_document([], lib.prefixes, insts), a.output)
    return 0


def cmd_run(a) -> int:
    q = parse_query(Path(a.query).read_text(encoding="utf8"), Path(a.query).stem)
    g = run_query(q, _rows(a))
    for p, ns in q.prefixes.items():
        g.bind(p, ns)
    _out(g.serialize(format="nt" if a.ntriples else "turtle"), a.output)
    return 0


def cmd_expand(a) -> int:
    lib = load_library(a.library)
    insts = []
    for f in a.instances:
        doc = parse_stottr(Path(f).read_text(encoding="utf8"))
        insts += doc.instances
        for p, ns in doc.prefixes.items():
            lib.prefixes.add(p, ns, override=False)
    g = expand(lib, insts)
    for p, ns in lib.prefixes.items():
        g.bind(p, ns)
    _out(g.serialize(format="nt" if a.ntriples else "turtle"), a.output)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ottr-tarql", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("decompose", help="TARQL queries -> shared OTTR template library + one root template per query")
    p.add_argument("queries", nargs="+")
    p.add_argument("-o", "--output", required=True, help="output directory")
    p.add_argument("--template-ns", default="http://example.org/ottr/template/")
    p.add_argument("--query-ns", default="http://example.org/ottr/query/")
    p.add_argument("--no-factor", action="store_true", help="do not rewrite shapes to reuse smaller shapes")
    p.set_defaults(fn=cmd_decompose)

    p = sub.add_parser("compose", help="OTTR template -> TARQL CONSTRUCT query")
    p.add_argument("library", nargs="+", help="stOTTR files or directories")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("-T", "--template", help="template IRI or prefixed name")
    g.add_argument("--all", action="store_true", help="compose every root template made by decompose")
    p.add_argument("-o", "--output", help="output file (directory with --all)")
    p.add_argument("--no-infer-binds", action="store_true", help="do not convert typed parameters from CSV strings")
    p.set_defaults(fn=cmd_compose)

    p = sub.add_parser("shapes", help="OTTR templates -> SHACL shapes for the RDF they produce")
    p.add_argument("library", nargs="+", help="stOTTR files or directories")
    p.add_argument(
        "-T", "--template", action="append",
        help="root template (repeatable); default: the roots made by decompose, else every template",
    )
    p.add_argument("-o", "--output")
    p.add_argument("--shapes-ns", default=DEFAULT_SHAPES_NS, help="namespace for the shape IRIs")
    p.add_argument("--max-counts", action="store_true", help="add sh:maxCount, assuming one instance builds each node")
    p.add_argument(
        "--allow-subtypes", action="store_true",
        help="accept datatype subtypes as OTTR does (e.g. xsd:int where xsd:integer is declared)",
    )
    p.set_defaults(fn=cmd_shapes)

    p = sub.add_parser("instances", help="CSV rows -> stOTTR instances of a template (for Lutra)")
    p.add_argument("library", nargs="+")
    p.add_argument("-T", "--template", required=True)
    p.add_argument("-o", "--output")
    _csv_opts(p)
    p.set_defaults(fn=cmd_instances)

    p = sub.add_parser("run", help="run a TARQL query over a CSV with rdflib (oxi-gen semantics)")
    p.add_argument("-q", "--query", required=True)
    p.add_argument("-o", "--output")
    p.add_argument("--ntriples", action="store_true")
    _csv_opts(p)
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("expand", help="expand stOTTR instances with the built-in expander")
    p.add_argument("library", nargs="+")
    p.add_argument("--instances", nargs="+", required=True)
    p.add_argument("-o", "--output")
    p.add_argument("--ntriples", action="store_true")
    p.set_defaults(fn=cmd_expand)

    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except (OSError, KeyError, ValueError) as e:  # Unsupported and SyntaxErr are ValueErrors
        raise SystemExit(f"ottr-tarql {a.cmd}: {e}") from None


if __name__ == "__main__":
    raise SystemExit(main())

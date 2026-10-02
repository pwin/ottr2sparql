"""Execution helpers built on rdflib.

* :func:`run_query` - evaluate a TARQL query over CSV rows the way oxi-gen does
  (one solution per row, empty cells unbound, ``?ROWNUM``, ``tarql:`` functions).
* :func:`generate_instances` - turn CSV rows into stOTTR instances of a root
  template, by evaluating the WHERE clause of the composed query.
* :func:`expand` - expand OTTR instances to triples (a convenience; Lutra is the
  reference implementation).
"""

from __future__ import annotations

import csv
import io
from dataclasses import replace

import rdflib
from rdflib.plugins.sparql.operators import register_custom_function
from rdflib.plugins.sparql.sparql import SPARQLError

from .compose import _Expander, compose
from .ottr import Instance, Library
from .sparql import Raw, TarqlQuery, serialize_query
from .terms import IRI, NONE, XSD, BNode, ListTerm, Literal, escape_string

TARQL_NAMESPACES = ("https://semanticarts.com/tarql/", "http://tarql.github.io/tarql#")
_prefixes: dict[str, str] = {}


def _expand_prefixed_name(qname):
    s = str(qname)
    if not s or ":" not in s:
        raise SPARQLError(f"malformed qname {s!r}")
    p, _, local = s.partition(":")
    if p not in _prefixes:
        raise SPARQLError(f"unknown prefix {p!r}")
    return rdflib.URIRef(_prefixes[p] + local)


def _expand_prefix(prefix):
    p = str(prefix)
    if p not in _prefixes:
        raise SPARQLError(f"unknown prefix {p!r}")
    return rdflib.Literal(_prefixes[p])


for _ns in TARQL_NAMESPACES:
    register_custom_function(rdflib.URIRef(_ns + "expandPrefixedName"), _expand_prefixed_name, override=True)
    register_custom_function(rdflib.URIRef(_ns + "expandPrefix"), _expand_prefix, override=True)


# ---------------------------------------------------------------------------- CSV


def read_csv(text: str, delimiter: str = ",", header: bool = True) -> list[dict[str, str]]:
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = [r for r in reader if r]
    if not rows:
        return []
    if header:
        cols, rows = [c.strip().replace('"', "") for c in rows[0]], rows[1:]
    else:
        letters = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
        cols = list(letters[: max(len(r) for r in rows)])
    return [dict(zip(cols, r)) for r in rows]


def _values_clause(query: TarqlQuery, rows: list[dict[str, str]]) -> str:
    text = serialize_query(query)
    mentioned = {c for r in rows for c in r if f"?{c}" in text or f"${c}" in text}
    cols = sorted(mentioned)
    with_rownum = "?ROWNUM" in text
    head = " ".join(f"?{c}" for c in cols) + (" ?ROWNUM" if with_rownum else "")
    lines = []
    for n, r in enumerate(rows, 1):
        vals = [escape_string(r[c]) if r.get(c, "").strip() else "UNDEF" for c in cols]
        if with_rownum:
            vals.append(str(n))
        lines.append("(" + " ".join(vals) + ")")
    if not head.strip():
        return "VALUES () { " + " ".join("()" for _ in rows) + " }"
    return f"VALUES ({head}) {{\n" + "\n".join("  " + ln for ln in lines) + "\n}"


def _with_rows(query: TarqlQuery, rows: list[dict[str, str]]) -> TarqlQuery:
    pm = query.prefixes.copy()
    if "tarql" not in pm.map:
        pm.add("tarql", TARQL_NAMESPACES[0])
    return replace(query, prefixes=pm, where=[Raw(_values_clause(query, rows))] + list(query.where), dataset="", header="")


def run_query(query: TarqlQuery, rows: list[dict[str, str]]) -> rdflib.Graph:
    """Evaluate a TARQL CONSTRUCT over CSV rows (oxi-gen semantics)."""
    q = _with_rows(query, rows)
    _prefixes.clear()
    _prefixes.update(q.prefixes.map)
    res = rdflib.Graph().query(serialize_query(q))
    return normalize(res.graph)


def select_rows(query: TarqlQuery, rows: list[dict[str, str]]) -> list[dict]:
    q = _with_rows(query, rows)
    _prefixes.clear()
    _prefixes.update(q.prefixes.map)
    text = serialize_query(q)
    text = text[: text.index("CONSTRUCT")] + "SELECT *" + text[text.index("\nWHERE {") :]
    return [dict(b) for b in rdflib.Graph().query(text).bindings]


# ---------------------------------------------------------------------------- OTTR instances


def from_rdflib(t):
    if isinstance(t, rdflib.URIRef):
        return IRI(str(t))
    if isinstance(t, rdflib.BNode):
        return BNode(str(t))
    if isinstance(t, rdflib.Literal):
        return Literal(str(t), str(t.datatype) if t.datatype else None, t.language)
    raise TypeError(t)


def to_rdflib(t):
    if isinstance(t, IRI):
        return rdflib.URIRef(t.value)
    if isinstance(t, BNode):
        return rdflib.BNode(t.label)
    if isinstance(t, Literal):
        if t.lang:
            return rdflib.Literal(t.lexical, lang=t.lang)
        return rdflib.Literal(t.lexical, datatype=rdflib.URIRef(t.datatype) if t.datatype else None)
    raise TypeError(t)


def generate_instances(lib: Library, template: str, rows: list[dict[str, str]]) -> list[Instance]:
    res = compose(lib, template)
    root = lib.get(lib.resolve(template))
    out = []
    for sol in select_rows(res.query, rows):
        sol = {str(k): v for k, v in sol.items()}
        args = [from_rdflib(sol[res.arg_vars[p.name]]) if res.arg_vars[p.name] in sol else NONE for p in root.params]
        out.append(Instance(root.iri, args))
    return out


def expand(lib: Library, instances: list[Instance]) -> rdflib.Graph:
    """Expand instances to RDF. Blank nodes in different instances are distinct
    unless they share a label (stOTTR document scope)."""
    exp = _Expander(lib, lib.prefixes)
    for inst in instances:
        doc_scope = {("bnode", b.label): BNode("doc_" + b.label) for b in _bnodes(inst.args)}
        exp.instance(inst, doc_scope, frozenset(), 0)
    g = rdflib.Graph()
    for s, p, o in exp.triples:
        g.add((to_rdflib(s), to_rdflib(p), to_rdflib(o)))
    return normalize(g)


def _bnodes(terms):
    for t in terms:
        if isinstance(t, BNode):
            yield t
        elif isinstance(t, ListTerm):
            yield from _bnodes(t.items)


def normalize(g: rdflib.Graph) -> rdflib.Graph:
    """Treat "x" and "x"^^xsd:string as the same literal (RDF 1.1)."""
    out = rdflib.Graph()
    xs = rdflib.URIRef(XSD + "string")
    for s, p, o in g:
        if isinstance(o, rdflib.Literal) and o.datatype == xs:
            o = rdflib.Literal(str(o))
        out.add((s, p, o))
    return out

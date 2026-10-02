"""RDF/OTTR term model shared by the SPARQL and stOTTR sides, plus rendering."""

from __future__ import annotations

import re
from dataclasses import dataclass

XSD = "http://www.w3.org/2001/XMLSchema#"
RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"
OWL = "http://www.w3.org/2002/07/owl#"
OTTR = "http://ns.ottr.xyz/0.4/"
# Vocabulary for the annotations that carry TARQL-only information (WHERE clause,
# prefixes, source file) on a generated root template. Change it here if you mint
# a permanent namespace.
TQ = "http://example.org/ottr-tarql#"

RDF_TYPE = RDF + "type"
OTTR_TRIPLE = OTTR + "Triple"
OTTR_IRI = OTTR + "IRI"


@dataclass(frozen=True)
class IRI:
    value: str


@dataclass(frozen=True)
class BNode:
    label: str


@dataclass(frozen=True)
class Var:
    name: str


@dataclass(frozen=True)
class Literal:
    lexical: str
    datatype: str | None = None  # None (with lang None) means xsd:string
    lang: str | None = None

    def __post_init__(self):
        if self.datatype == XSD + "string":
            object.__setattr__(self, "datatype", None)
        if self.lang is not None:
            object.__setattr__(self, "datatype", None)


class _NoneTerm:
    _inst = None

    def __new__(cls):
        if cls._inst is None:
            cls._inst = super().__new__(cls)
        return cls._inst

    def __repr__(self):
        return "NONE"


NONE = _NoneTerm()


@dataclass(frozen=True)
class ListTerm:
    items: tuple


Term = IRI | BNode | Var | Literal | _NoneTerm | ListTerm


class PrefixMap:
    """Ordered prefix -> namespace map with longest-namespace compaction."""

    _LOCAL = re.compile(r"^(?:[\w%](?:[\w.\-%]*[\w\-%])?)?$")

    def __init__(self, items: dict[str, str] | None = None):
        self.map: dict[str, str] = dict(items or {})

    def copy(self) -> "PrefixMap":
        return PrefixMap(self.map)

    def add(self, prefix: str, ns: str, override: bool = True) -> None:
        if override or prefix not in self.map:
            self.map[prefix] = ns

    def expand(self, prefix: str, local: str) -> str:
        if prefix not in self.map:
            raise KeyError(f"undeclared prefix '{prefix}:'")
        return self.map[prefix] + local

    def compact(self, iri: str) -> str | None:
        best = None
        for p, ns in self.map.items():
            if iri.startswith(ns) and self._LOCAL.match(iri[len(ns):]):
                if best is None or len(ns) > len(self.map[best]):
                    best = p
        if best is None:
            return None
        return f"{best}:{iri[len(self.map[best]):]}"

    def items(self):
        return self.map.items()


def escape_string(s: str) -> str:
    out = []
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


_INT = re.compile(r"^[+-]?\d+$")
_DEC = re.compile(r"^[+-]?\d*\.\d+$")
_DBL = re.compile(r"^[+-]?(\d+\.\d*|\.?\d+)[eE][+-]?\d+$")


def render_iri(iri: str, prefixes: PrefixMap) -> str:
    if iri == RDF_TYPE and "rdf" not in prefixes.map:
        return "<" + iri + ">"
    return prefixes.compact(iri) or "<" + iri + ">"


def render_term(t, prefixes: PrefixMap, syntax: str = "sparql") -> str:
    if isinstance(t, Var):
        return "?" + t.name
    if isinstance(t, IRI):
        return render_iri(t.value, prefixes)
    if isinstance(t, BNode):
        return "_:" + t.label
    if isinstance(t, Literal):
        if t.lang:
            return escape_string(t.lexical) + "@" + t.lang
        dt = t.datatype
        if dt is None:
            return escape_string(t.lexical)
        if dt == XSD + "integer" and _INT.match(t.lexical):
            return t.lexical
        if dt == XSD + "decimal" and _DEC.match(t.lexical):
            return t.lexical
        if dt == XSD + "double" and _DBL.match(t.lexical):
            return t.lexical
        if dt == XSD + "boolean" and t.lexical in ("true", "false"):
            return t.lexical
        return escape_string(t.lexical) + "^^" + render_iri(dt, prefixes)
    if t is NONE:
        if syntax != "stottr":
            raise ValueError("ottr:none has no SPARQL rendering")
        return "none"
    if isinstance(t, ListTerm):
        if syntax != "stottr":
            raise ValueError("lists have no TARQL rendering")
        return "(" + ", ".join(render_term(i, prefixes, syntax) for i in t.items) + ")"
    raise TypeError(f"cannot render {t!r}")


def local_name(iri: str) -> str:
    m = re.search(r"[^#/:]+$", iri)
    return m.group(0) if m else "x"


def safe_name(s: str) -> str:
    """A string usable as both an OTTR/SPARQL variable name and a PN_LOCAL."""
    s = re.sub(r"[^\w]", "_", s)
    if not s or not (s[0].isalpha() or s[0] == "_"):
        s = "v_" + s
    return s

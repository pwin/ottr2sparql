"""Parser and serializer for TARQL / oxi-gen style SPARQL CONSTRUCT queries.

Only the CONSTRUCT template is parsed into terms. The WHERE clause is split into
top-level ``BIND(expr AS ?v)`` clauses (kept as expression text) and opaque
"raw" clauses (FILTER, OPTIONAL, VALUES, ...) that are carried verbatim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

from .lexer import Stream, SyntaxErr, Token, tokenize
from .terms import IRI, OTTR_IRI, RDF, RDF_TYPE, XSD, BNode, Literal, PrefixMap, Var, render_term


class Unsupported(ValueError):
    """A construct that has no OTTR (or no TARQL) counterpart."""


@dataclass
class Bind:
    var: str
    expr: str


@dataclass
class Raw:
    text: str


@dataclass
class TarqlQuery:
    prefixes: PrefixMap
    triples: list[tuple]  # (s, p, o) of Var | IRI | BNode | Literal
    where: list[Bind | Raw] = field(default_factory=list)
    base: str | None = None
    dataset: str = ""  # anything between the template and WHERE (e.g. FROM <file.csv>)
    tail: str = ""  # solution modifiers after the WHERE group
    header: str = ""  # comment block to emit at the top
    name: str = "query"

    # ---- derived information -------------------------------------------------
    def bind_targets(self) -> dict[str, str]:
        return {c.var: c.expr for c in self.where if isinstance(c, Bind)}

    def where_vars(self) -> set[str]:
        out: set[str] = set()
        for c in self.where:
            text = c.expr if isinstance(c, Bind) else c.text
            out |= {t.value for t in tokenize(text) if t.kind == "VAR"}
        return out

    def template_vars(self) -> list[str]:
        seen: list[str] = []
        for tr in self.triples:
            for t in tr:
                if isinstance(t, Var) and t.name not in seen:
                    seen.append(t.name)
        return seen


# ---------------------------------------------------------------------------- parsing


def parse_query(text: str, name: str = "query") -> TarqlQuery:
    s = Stream(tokenize(text, "sparql"), text)
    prefixes = PrefixMap()
    base = None
    while True:
        if s.at("NAME", "PREFIX", ci=True):
            s.next()
            p = s.expect("PNAME")
            if p.local:
                raise SyntaxErr(f"bad PREFIX declaration {p.value}")
            prefixes.add(p.prefix, _resolve(s.expect("IRIREF").value, base))
        elif s.at("NAME", "BASE", ci=True):
            s.next()
            base = s.expect("IRIREF").value
        else:
            break
    s.expect("NAME", "CONSTRUCT", ci=True)
    if not s.at("PUNCT", "{"):
        raise Unsupported("only CONSTRUCT { template } WHERE { ... } queries are supported")
    s.next()
    ctx = _Ctx(prefixes, base)
    triples = _parse_triples_block(s, ctx)
    tpl_close = s.expect("PUNCT", "}")

    # optional dataset clause (FROM ...) and optional WHERE keyword
    ds_start = ds_end = tpl_close.end
    while not s.at("PUNCT", "{"):
        if s.eof():
            raise SyntaxErr("missing WHERE clause")
        t = s.next()
        if not (t.kind == "NAME" and t.value.upper() == "WHERE"):
            ds_end = t.end
    dataset = text[ds_start:ds_end].strip()
    s.expect("PUNCT", "{")
    where, close = _parse_where(s, text)
    tail = text[close.end :].strip()
    return TarqlQuery(prefixes, triples, where, base, dataset, tail, name=name)


def _resolve(iri: str, base: str | None) -> str:
    return urljoin(base, iri) if base and not re.match(r"^[A-Za-z][\w+.\-]*:", iri) else iri


class _Ctx:
    def __init__(self, prefixes: PrefixMap, base: str | None):
        self.prefixes, self.base, self.anon = prefixes, base, 0

    def fresh(self) -> BNode:
        self.anon += 1
        return BNode(f"anon{self.anon}")


def _parse_triples_block(s: Stream, ctx: _Ctx) -> list[tuple]:
    out: list[tuple] = []
    while not s.at("PUNCT", "}"):
        if s.at("PUNCT", "."):
            s.next()
            continue
        if s.at("PUNCT", "["):
            s.next()
            subj = ctx.fresh()
            if s.at("PUNCT", "]"):
                s.next()
            else:
                _parse_pol(s, ctx, subj, out)
                s.expect("PUNCT", "]")
            if not (s.at("PUNCT", ".") or s.at("PUNCT", "}")):
                _parse_pol(s, ctx, subj, out)
        else:
            subj = _parse_term(s, ctx, out)
            _parse_pol(s, ctx, subj, out)
        if not s.at("PUNCT", "}"):
            s.expect("PUNCT", ".")
    return out


def _parse_pol(s: Stream, ctx: _Ctx, subj, out: list) -> None:
    while True:
        if s.at("NAME", "a"):
            s.next()
            pred = IRI(RDF_TYPE)
        else:
            pred = _parse_term(s, ctx, out)
            if isinstance(pred, (Literal, BNode)):
                raise SyntaxErr(f"invalid predicate {pred}")
        while True:
            obj = _parse_term(s, ctx, out)
            if s.at("PUNCT", "~") or s.at("PUNCT", "{|"):
                raise Unsupported("RDF 1.2 syntax ('~' reifiers, '{| |}' annotations) is not supported: this tool works with RDF 1.1 and SPARQL 1.1")
            out.append((subj, pred, obj))
            if s.at("PUNCT", ","):
                s.next()
                continue
            break
        while s.at("PUNCT", ";"):
            s.next()
        if s.at("PUNCT", ".") or s.at("PUNCT", "}") or s.at("PUNCT", "]"):
            return


def _parse_term(s: Stream, ctx: _Ctx, out: list):
    t = s.next()
    if t.kind == "VAR":
        return Var(t.value)
    if t.kind == "IRIREF":
        return IRI(_resolve(t.value, ctx.base))
    if t.kind == "PNAME":
        try:
            return IRI(ctx.prefixes.expand(t.prefix, t.local))
        except KeyError as e:
            raise SyntaxErr(str(e)) from None
    if t.kind == "BNODE":
        return BNode(t.value)
    if t.kind == "STRING":
        return _literal_tail(s, ctx, t.value)
    if t.kind == "NUMBER":
        return Literal(t.value, XSD + t.numtype)
    if t.kind == "NAME" and t.value in ("true", "false"):
        return Literal(t.value, XSD + "boolean")
    if t.kind == "PUNCT" and t.value == "[":
        node = ctx.fresh()
        if not s.at("PUNCT", "]"):
            _parse_pol(s, ctx, node, out)
        s.expect("PUNCT", "]")
        return node
    if t.kind == "PUNCT" and t.value == "(":
        raise Unsupported("RDF collections in CONSTRUCT templates are not supported")
    if t.kind == "PUNCT" and t.value == "<<":
        raise Unsupported("RDF 1.2 triple terms ('<< >>') are not supported: this tool works with RDF 1.1 and SPARQL 1.1")
    raise SyntaxErr(f"unexpected token {t.value!r} in CONSTRUCT template")


def _literal_tail(s: Stream, ctx: _Ctx, lex: str) -> Literal:
    if s.at("LANGTAG"):
        return Literal(lex, lang=s.next().value)
    if s.at("DTYPE"):
        s.next()
        dt = _parse_term(s, ctx, [])
        if not isinstance(dt, IRI):
            raise SyntaxErr("datatype must be an IRI")
        return Literal(lex, dt.value)
    return Literal(lex)


def _parse_where(s: Stream, text: str) -> tuple[list[Bind | Raw], Token]:
    """Split the WHERE group into top-level BINDs and verbatim raw chunks."""
    clauses: list[Bind | Raw] = []
    raw_start: Token | None = None
    raw_end: Token | None = None

    def flush():
        nonlocal raw_start, raw_end
        if raw_start is not None:
            chunk = text[raw_start.start : raw_end.end].strip()
            if chunk.strip(" .\n\t"):
                clauses.append(Raw(chunk))
        raw_start = raw_end = None

    depth = 0
    while True:
        t = s.peek()
        if t is None:
            raise SyntaxErr("unterminated WHERE clause")
        if depth == 0 and t.kind == "PUNCT" and t.value == "}":
            flush()
            return clauses, s.next()
        if depth == 0 and t.kind == "NAME" and t.value.upper() == "BIND" and s.at("PUNCT", "(", k=1):
            flush()
            s.next()
            open_paren = s.next()
            pdepth, as_tok, var_tok = 1, None, None
            while pdepth:
                u = s.next()
                if u.kind == "PUNCT" and u.value in "([{":
                    pdepth += 1
                elif u.kind == "PUNCT" and u.value in ")]}":
                    pdepth -= 1
                elif pdepth == 1 and u.kind == "NAME" and u.value.upper() == "AS":
                    as_tok = u
                    var_tok = s.expect("VAR")
            if as_tok is None:
                raise SyntaxErr("BIND without AS")
            clauses.append(Bind(var_tok.value, text[open_paren.end : as_tok.start].strip()))
            if s.at("PUNCT", "."):
                s.next()
            continue
        s.next()
        if t.kind == "PUNCT" and t.value in "{([":
            depth += 1
        elif t.kind == "PUNCT" and t.value in "})]":
            depth -= 1
        if raw_start is None:
            raw_start = t
        raw_end = t


# ---------------------------------------------------------------------------- type inference

_IRI_FUNCS = {"IRI", "URI"}
_STR_FUNCS = {"STR", "CONCAT", "LCASE", "UCASE", "SUBSTR", "REPLACE", "STRBEFORE", "STRAFTER", "ENCODE_FOR_URI", "STRUUID"}


def infer_expr_type(expr: str, prefixes: PrefixMap, var_type=None) -> str | None:
    """Best-effort OTTR type (IRI) of a BIND expression, or None if unknown.

    ``COALESCE(a, b)`` and ``IF(c, a, b)`` have the type that ``a`` and ``b`` agree
    on. ``var_type(name)`` gives the type of a variable used as such a branch."""
    try:
        toks = tokenize(expr)
    except SyntaxErr:
        return None
    return _expr_type(toks, prefixes, var_type or (lambda name: None))


def _call_args(toks: list[Token]) -> list[list[Token]] | None:
    """The argument token lists of ``f(a, b, ...)``, or None if ``toks`` is not one call."""
    if len(toks) < 3 or not (toks[1].kind == "PUNCT" and toks[1].value == "("):
        return None
    args, current, depth = [], [], 0
    for i, t in enumerate(toks[2:], start=2):
        if t.kind == "PUNCT" and t.value in "([{":
            depth += 1
        elif t.kind == "PUNCT" and t.value in ")]}":
            if depth == 0:
                if i != len(toks) - 1:
                    return None
                return args + [current] if current else args
            depth -= 1
        elif depth == 0 and t.kind == "PUNCT" and t.value == ",":
            args.append(current)
            current = []
            continue
        current.append(t)
    return None


def _term_type(t: Token, prefixes: PrefixMap, var_type) -> str | None:
    if t.kind in ("IRIREF", "PNAME"):
        return OTTR_IRI
    if t.kind == "STRING":
        return XSD + "string"
    if t.kind == "NUMBER":
        return XSD + t.numtype
    if t.kind == "NAME" and t.value in ("true", "false"):
        return XSD + "boolean"
    if t.kind == "VAR":
        return var_type(t.value)
    return None


def _expr_type(toks: list[Token], prefixes: PrefixMap, var_type) -> str | None:
    if len(toks) == 1:
        return _term_type(toks[0], prefixes, var_type)
    if len(toks) == 2 and toks[0].kind == "STRING" and toks[1].kind == "LANGTAG":
        return RDF + "langString"
    if len(toks) == 3 and toks[0].kind == "STRING" and toks[1].kind == "DTYPE":
        dt = toks[2]
        if dt.kind == "PNAME" and dt.prefix in prefixes.map:
            return prefixes.expand(dt.prefix, dt.local)
        return dt.value if dt.kind == "IRIREF" else None
    if len(toks) < 2 or not (toks[1].kind == "PUNCT" and toks[1].value == "("):
        return None
    head = toks[0]
    if head.kind == "NAME":
        f = head.value.upper()
        if f in ("COALESCE", "IF"):
            args = _call_args(toks)
            if not args or (f == "IF" and len(args) != 3):
                return None
            types = {_expr_type(a, prefixes, var_type) for a in (args if f == "COALESCE" else args[1:])}
            return types.pop() if len(types) == 1 else None
        if f == "BNODE":
            return OTTR_IRI
        if f in _IRI_FUNCS:
            return OTTR_IRI
        if f in _STR_FUNCS:
            return XSD + "string"
        if f == "STRDT":
            dt = toks[-2]
            if dt.kind == "PNAME" and dt.prefix in prefixes.map:
                return prefixes.expand(dt.prefix, dt.local)
            if dt.kind == "IRIREF":
                return dt.value
        if f == "STRLANG":
            return RDF + "langString"
        return None
    if head.kind == "PNAME":
        if head.local in ("expandPrefixedName",):
            return OTTR_IRI
        if head.prefix in prefixes.map and prefixes.map[head.prefix] == XSD:
            return XSD + head.local
        if head.prefix not in prefixes.map and head.prefix == "xsd":
            return XSD + head.local
    return None


# ---------------------------------------------------------------------------- serialization


def serialize_query(q: TarqlQuery) -> str:
    pm = q.prefixes
    lines: list[str] = []
    if q.header:
        lines += ["# " + ln if ln else "#" for ln in q.header.splitlines()]
    if q.base:
        lines.append(f"BASE <{q.base}>")
    for p, ns in pm.items():
        lines.append(f"PREFIX {p}: <{ns}>")
    if lines:
        lines.append("")
    lines.append("CONSTRUCT {")
    lines += _render_template(q.triples, pm)
    lines.append("}")
    if q.dataset:
        lines.append(q.dataset)
    lines.append("WHERE {")
    for c in q.where:
        if isinstance(c, Bind):
            lines.append(f"  BIND({c.expr} AS ?{c.var})")
        else:
            lines += ["  " + ln for ln in c.text.splitlines()]
    lines.append("}")
    if q.tail:
        lines.append(q.tail)
    return "\n".join(lines) + "\n"


def _render_template(triples: list[tuple], pm: PrefixMap) -> list[str]:
    groups: dict = {}
    for s, p, o in triples:
        groups.setdefault(s, {}).setdefault(p, [])
        if o not in groups[s][p]:
            groups[s][p].append(o)
    out = []
    for s, preds in groups.items():
        out.append("  " + render_term(s, pm))
        plist = list(preds.items())
        for i, (p, objs) in enumerate(plist):
            ptxt = "a" if p == IRI(RDF_TYPE) else render_term(p, pm)
            otxt = ", ".join(render_term(o, pm) for o in objs)
            end = " ." if i == len(plist) - 1 else " ;"
            out.append(f"      {ptxt} {otxt}{end}")
    return out

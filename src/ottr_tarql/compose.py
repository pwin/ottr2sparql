"""OTTR template -> TARQL CONSTRUCT query.

The template is expanded symbolically: its parameters become SPARQL variables
and nested instances are expanded down to ``ottr:Triple`` instances, which
become the CONSTRUCT template.  OTTR instance semantics that SPARQL's
"skip triples with unbound variables" rule does not give for free are encoded in
the WHERE clause:

* default values      ->  ``BIND(COALESCE(?arg, default) AS ?arg_dN)``
* mandatory parameter ->  an instance is dropped when a mandatory argument is
  unbound. Triples of that instance that do not mention the variable are
  guarded: their predicate is replaced by ``?_gN`` bound with
  ``IF(BOUND(?arg), pred, ?_unbound)``.
* list expanders      ->  unrolled at compose time (lists must be constants).

If the root template carries ``tq:`` annotations (made by ``decompose``) they
restore the original WHERE clause and prefixes. Otherwise the parameters are
read from CSV columns of the same name, with IRI- and datatype-typed
parameters converted by generated BINDs.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from .decompose import TQ_BASE, TQ_BIND, TQ_DATASET, TQ_MODIFIERS, TQ_PREFIX, TQ_SOURCE, TQ_WHERE
from .ottr import IRI_TYPES, Instance, Library, ListType, LubType, Template
from .sparql import Bind, Raw, TarqlQuery, Unsupported
from .terms import IRI, NONE, OTTR, OTTR_TRIPLE, RDF, TQ, XSD, BNode, ListTerm, Literal, PrefixMap, Var, local_name, render_term


@dataclass
class ComposeResult:
    query: TarqlQuery
    arg_vars: dict[str, str]  # root parameter name -> query variable carrying its value
    columns: list[str] = field(default_factory=list)  # CSV columns the query reads


class _Expander:
    """Symbolic expansion of template instances into (s, p, o) patterns."""

    def __init__(self, lib: Library, prefixes: PrefixMap, max_depth: int = 64):
        self.lib, self.pm, self.max_depth = lib, prefixes, max_depth
        self.triples: list[tuple] = []
        self._seen: set[tuple] = set()
        self.binds: list[Bind] = []
        self.always_bound: set[str] = set()
        self._guards: dict = {}
        self._n = itertools.count(1)

    # -- instances --------------------------------------------------------------
    def instance(self, inst: Instance, subst: dict, required: frozenset, depth: int) -> None:
        if depth > self.max_depth:
            raise Unsupported(f"template nesting deeper than {self.max_depth} (cyclic templates?)")
        args = [self._subst(a, subst) for a in inst.args]
        for arglist in self._expand_lists(inst, args):
            self.apply(inst.template, arglist, required, depth)

    def apply(self, iri: str, args: list, required: frozenset, depth: int) -> None:
        t = self.lib.get(iri)
        if len(args) != len(t.params):
            raise ValueError(f"<{iri}> takes {len(t.params)} arguments, got {len(args)}")
        if iri == OTTR_TRIPLE:
            if any(a is NONE for a in args):
                return
            self.emit(*args, required)
            return
        if t.kind != "template":
            raise Unsupported(f"base template <{iri}> has no SPARQL translation (only ottr:Triple does)")
        fresh = self._fresh_bnodes(t)
        sub, req = dict(fresh), set(required)
        for p, a in zip(t.params, args):
            if a is NONE:
                if p.default is not None:
                    a = self._subst(p.default, fresh)
                elif not p.optional:
                    return  # mandatory parameter without value: the instance is removed
            elif isinstance(a, Var) and a.name not in self.always_bound:
                if p.default is not None:
                    a = self._coalesce(a, self._subst(p.default, fresh))
                elif not p.optional:
                    req.add(a.name)
            sub[p.name] = a
        for inner in t.body or []:
            self.instance(inner, sub, frozenset(req), depth + 1)

    def emit(self, s, p, o, required: frozenset) -> None:
        if isinstance(o, ListTerm):
            o = self._rdf_list(o, required)
        for t in (s, p):
            if isinstance(t, ListTerm):
                raise Unsupported("a list cannot be the subject or predicate of a triple")
        present = {t.name for t in (s, p, o) if isinstance(t, Var)}
        missing = required - present
        if missing:
            p = self._guard(p, frozenset(missing))
        tr = (s, p, o)
        if tr not in self._seen:
            self._seen.add(tr)
            self.triples.append(tr)

    # -- helpers ------------------------------------------------------------------
    def _subst(self, term, subst: dict):
        if isinstance(term, Var):
            if term.name not in subst:
                raise ValueError(f"unbound template variable ?{term.name}")
            return subst[term.name]
        if isinstance(term, BNode):
            return subst.get(("bnode", term.label), term)
        if isinstance(term, ListTerm):
            return ListTerm(tuple(self._subst(i, subst) for i in term.items))
        return term

    def _fresh_bnodes(self, t: Template) -> dict:
        labels: set[str] = set()

        def walk(x):
            if isinstance(x, BNode):
                labels.add(x.label)
            elif isinstance(x, ListTerm):
                for i in x.items:
                    walk(i)

        for inst in t.body or []:
            for a in inst.args:
                walk(a)
        for p in t.params:
            if p.default is not None:
                walk(p.default)
        return {("bnode", lb): BNode(f"b{next(self._n)}") for lb in sorted(labels)}

    def _expand_lists(self, inst: Instance, args: list) -> list[list]:
        if not inst.expander:
            return [args]
        lists = {}
        for i, (a, f) in enumerate(zip(args, inst.expand_flags)):
            if not f:
                continue
            if a is NONE:
                return []
            if not isinstance(a, ListTerm):
                raise Unsupported(
                    "a list expander over a parameter needs list values per row, which TARQL rows do not have; "
                    "only constant lists can be expanded (see docs/DESIGN.md, 'Lists')"
                )
            lists[i] = list(a.items)
        if inst.expander == "cross":
            combos = itertools.product(*lists.values())
        elif inst.expander == "zipMin":
            combos = zip(*lists.values())
        else:
            combos = itertools.zip_longest(*lists.values(), fillvalue=NONE)
        out = []
        for combo in combos:
            new = list(args)
            for i, v in zip(lists.keys(), combo):
                new[i] = v
            out.append(new)
        return out

    def _coalesce(self, v: Var, default) -> Var:
        if isinstance(default, ListTerm):
            raise Unsupported("list-valued defaults are not supported")
        expr = "BNODE()" if isinstance(default, BNode) else render_term(default, self.pm)
        nv = f"{v.name}_d{next(self._n)}"
        self.binds.append(Bind(nv, f"COALESCE(?{v.name}, {expr})"))
        self.always_bound.add(nv)
        return Var(nv)

    def _guard(self, p, missing: frozenset):
        key = (p, missing)
        if key not in self._guards:
            gv = f"_g{next(self._n)}"
            cond = " && ".join(f"BOUND(?{m})" for m in sorted(missing))
            self.binds.append(Bind(gv, f"IF({cond}, {render_term(p, self.pm)}, ?_unbound)"))
            self._guards[key] = Var(gv)
        return self._guards[key]

    def _rdf_list(self, lst: ListTerm, required: frozenset):
        if not lst.items:
            return IRI(RDF + "nil")
        nodes = [BNode(f"l{next(self._n)}") for _ in lst.items]
        for i, (node, item) in enumerate(zip(nodes, lst.items)):
            if item is NONE:
                raise Unsupported("none inside a list")
            self.emit(node, IRI(RDF + "first"), item, required)
            nxt = nodes[i + 1] if i + 1 < len(nodes) else IRI(RDF + "nil")
            self.emit(node, IRI(RDF + "rest"), nxt, required)
        return nodes[0]


def _str(inst: Instance, i: int) -> str:
    a = inst.args[i]
    if isinstance(a, Literal):
        return a.lexical
    if isinstance(a, IRI):
        return a.value
    raise ValueError(f"unexpected annotation argument {a!r}")


def _is_tq(inst: Instance) -> bool:
    return inst.template.startswith(TQ)


def compose(lib: Library, template_iri: str, infer_binds: bool = True) -> ComposeResult:
    root = lib.get(lib.resolve(template_iri))
    if root.kind != "template":
        raise Unsupported(f"<{root.iri}> is a {root.kind}, not a template with a pattern")
    tq = [a for a in root.annotations if _is_tq(a)]
    where: list = []
    base = dataset = tail = None
    arg_vars: dict[str, str] = {}
    columns: list[str] = []

    if tq:  # round trip: restore what decompose recorded
        pm = PrefixMap()
        for a in tq:
            if a.template == TQ_PREFIX:
                pm.add(_str(a, 0), _str(a, 1))
            elif a.template == TQ_BASE:
                base = _str(a, 0)
            elif a.template == TQ_BIND:
                where.append(Bind(_str(a, 0), _str(a, 1)))
            elif a.template == TQ_WHERE:
                where.append(Raw(_str(a, 0)))
            elif a.template == TQ_DATASET:
                dataset = _str(a, 0)
            elif a.template == TQ_MODIFIERS:
                tail = _str(a, 0)
        arg_vars = {p.name: p.name for p in root.params}
        targets = {c.var for c in where if isinstance(c, Bind)}
        columns = [p.name for p in root.params if p.name not in targets and p.name != "ROWNUM"]
    else:
        pm = PrefixMap({p: ns for p, ns in lib.prefixes.items() if ns not in (OTTR, TQ)})
        taken = {p.name for p in root.params}
        for p in root.params:
            v = p.name
            columns.append(p.name)
            if infer_binds and p.type is not None:
                expr, suffix = _conversion(p.name, p.type, pm)
                if expr:
                    v = f"{p.name}_{suffix}"
                    while v in taken:
                        v += "_"
                    taken.add(v)
                    where.append(Bind(v, expr))
            arg_vars[p.name] = v

    exp = _Expander(lib, pm)
    args = [Var(arg_vars[p.name]) for p in root.params]
    exp.apply(root.iri, args, frozenset(), 0)
    where += exp.binds

    src = next((a for a in tq if a.template == TQ_SOURCE), None)
    name = _str(src, 0).rsplit(".", 1)[0] if src else local_name(root.iri)
    header = ""
    if not tq:
        header = (
            f"Generated from OTTR template <{root.iri}>.\n"
            f"CSV columns: {', '.join(columns) if columns else '(none)'}"
        )
    q = TarqlQuery(pm, exp.triples, where, base, dataset or "", tail or "", header, name)
    return ComposeResult(q, arg_vars, columns)


def _conversion(col: str, t, pm: PrefixMap) -> tuple[str | None, str]:
    """A BIND expression turning a CSV string into a value of OTTR type ``t``."""
    if isinstance(t, ListType):
        raise Unsupported(
            f"parameter ?{col} has list type; a TARQL row has one value per column. "
            "Use a constant list or split the column upstream (e.g. oxi-gen --split)."
        )
    if isinstance(t, LubType):
        t = t.inner
    if t in IRI_TYPES:
        return (
            f'IF(CONTAINS(STR(?{col}), "://"), IRI(?{col}), tarql:expandPrefixedName(?{col}))',
            "iri",
        )
    if isinstance(t, str) and t.startswith(XSD) and t != XSD + "string":
        if "xsd" not in pm.map:
            pm.add("xsd", XSD)
        return f"STRDT(?{col}, {render_term(IRI(t), pm)})", "typed"
    return None, ""


def roots_in(lib: Library) -> list[str]:
    """Templates that carry a tq:Source annotation (i.e. were made by decompose)."""
    return [iri for iri, t in lib.templates.items() if t.annotations_of(TQ_SOURCE)]

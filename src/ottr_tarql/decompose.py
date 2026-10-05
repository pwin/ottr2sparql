"""TARQL CONSTRUCT queries -> OTTR templates.

Each query becomes a *root* template whose parameters are the variables of its
CONSTRUCT template. The CONSTRUCT template is cut into one *shape* per subject
node; structurally identical shapes (up to variable renaming) across all input
queries become a single shared template. Optionally (``factor=True``) a shape
that strictly contains another shape is rewritten to call it.

The parts of a query OTTR cannot express (the WHERE clause, prefix declarations
that ``tarql:expandPrefixedName`` depends on, dataset and solution modifiers)
are kept as ``tq:`` annotation instances on the root template, so that
:func:`ottr_tarql.compose.compose` can rebuild an equivalent query.

Semantics preserved (see docs/DESIGN.md):
* a CONSTRUCT triple with an unbound variable is skipped  <->  ottr:Triple with
  a ``none`` argument is dropped (all three ottr:Triple parameters are mandatory);
* every root parameter is optional (any CSV cell may be empty);
* a shape's subject parameter is mandatory: when it is unbound every triple of
  the shape is skipped in TARQL, and the whole instance is dropped in OTTR;
* blank nodes are fresh per row in TARQL and fresh per instance in OTTR; blank
  nodes used by more than one shape are created in the root template and passed
  down as arguments.

A query made by :func:`ottr_tarql.compose.compose` carries guards: predicates
``?_gN`` bound to a constant only when the template's mandatory arguments are
bound. These are turned back into constant predicates, and the triples are
grouped by the variables they require instead of by subject. A group nests
inside the group whose requirements it extends, so the templates come back
nested, with the requirements as mandatory parameters.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from .lexer import SyntaxErr, tokenize
from .ottr import Instance, Param, Template, render_document
from .sparql import Bind, Raw, TarqlQuery, _call_args, infer_expr_type
from .terms import (
    IRI,
    OTTR,
    OTTR_IRI,
    OTTR_TRIPLE,
    RDF,
    RDF_TYPE,
    TQ,
    XSD,
    BNode,
    Literal,
    PrefixMap,
    Var,
    local_name,
    safe_name,
)

# ---------------------------------------------------------------------------- annotation vocabulary

TQ_SOURCE, TQ_PREFIX, TQ_BASE = TQ + "Source", TQ + "Prefix", TQ + "Base"
TQ_BIND, TQ_WHERE, TQ_DATASET, TQ_MODIFIERS = TQ + "Bind", TQ + "Where", TQ + "Dataset", TQ + "Modifiers"


def vocabulary_templates() -> list[Template]:
    s = XSD + "string"
    return [
        Template(TQ_SOURCE, [Param("file", s)], kind="signature"),
        Template(TQ_PREFIX, [Param("prefix", s), Param("namespace", s)], kind="signature"),
        Template(TQ_BASE, [Param("base", s)], kind="signature"),
        Template(TQ_BIND, [Param("variable", s), Param("expression", s)], kind="signature"),
        Template(TQ_WHERE, [Param("clause", s)], kind="signature"),
        Template(TQ_DATASET, [Param("clause", s)], kind="signature"),
        Template(TQ_MODIFIERS, [Param("clause", s)], kind="signature"),
    ]


# ---------------------------------------------------------------------------- shapes


@dataclass
class Shape:
    """A canonical (variable-free) form of one subject's triples."""

    key: tuple
    subject: tuple  # ('P', type) | ('C', term) | ('B',)
    items: list[tuple]  # [(slot_p, slot_o)], slot = ('S',) | ('P', i) | ('B', i) | ('C', term)
    types: list  # parameter index -> (type, non-blank)
    hints: list[str]  # parameter index -> suggested name
    name: str = ""
    params: list[Param] = field(default_factory=list)
    parent: tuple | None = None  # (Shape, {parent idx -> own idx}, set(own item idx covered))

    @property
    def subject_is_param(self) -> bool:
        return self.subject[0] == "P"

    def has_local_bnodes(self) -> bool:
        return self.subject[0] == "B" or any(s[0] == "B" for it in self.items for s in it)


def _tkey(t) -> tuple:
    if isinstance(t, IRI):
        return ("I", t.value)
    if isinstance(t, Literal):
        return ("L", t.lexical, t.datatype or "", t.lang or "")
    raise TypeError(t)


@dataclass
class QueryInfo:
    query: TarqlQuery
    types: dict[str, object]
    groups: list[tuple]  # (subject term, [(p, o)])
    shared_bnodes: set[str]
    nonblank: set[str]  # variables used as predicates (ottr:Triple's ?predicate is non-blank)
    # for a query made by compose: per triple, the variables its guard required
    requires: list[frozenset] | None = None


# ---------------------------------------------------------------------------- compose's guards


def _guard(expr: str, prefixes: PrefixMap) -> tuple[str, frozenset, str] | None:
    """A guard as compose writes it, ``IF(sameTerm(?a, ?a) && ..., <predicate>, ?unbound)``:
    (the predicate IRI, the variables it requires, the fallback variable)."""
    try:
        toks = tokenize(expr)
    except SyntaxErr:
        return None
    args = _call_args(toks)
    if not (args and len(args) == 3 and toks[0].kind == "NAME" and toks[0].value.upper() == "IF"):
        return None
    cond, then, other = args
    if len(then) != 1 or then[0].kind not in ("PNAME", "IRIREF") or len(other) != 1 or other[0].kind != "VAR":
        return None
    required, i = set(), 0
    while True:  # sameTerm(?v, ?v) && sameTerm(?w, ?w) ...
        p = cond[i : i + 6]
        if not (
            len(p) == 6 and p[0].kind == "NAME" and p[0].value.lower() == "sameterm"
            and [t.value for t in (p[1], p[3], p[5])] == ["(", ",", ")"]
            and p[2].kind == p[4].kind == "VAR" and p[2].value == p[4].value
        ):
            return None
        required.add(p[2].value)
        i += 6
        if i == len(cond):
            break
        if not (cond[i].kind == "PUNCT" and cond[i].value == "&&"):
            return None
        i += 1
    t = then[0]
    if t.kind == "PNAME":
        if t.prefix not in prefixes.map:
            return None
        return prefixes.expand(t.prefix, t.local), frozenset(required), other[0].value
    return t.value, frozenset(required), other[0].value


def recover_guards(q: TarqlQuery) -> tuple[TarqlQuery, list[frozenset] | None]:
    """Undo compose's guards: a triple ``(s, ?_gN, o)`` gets its constant predicate back,
    and the variables the guard required are returned per triple. The guard BINDs are
    dropped. Returns ``(q, None)`` if the query has no guards (or uses them otherwise)."""
    guards = {c.var: g for c in q.where if isinstance(c, Bind) and (g := _guard(c.expr, q.prefixes))}
    if not guards:
        return q, None
    # each guard variable must only be a predicate, and each fallback variable never bound
    elsewhere: set[str] = set()
    for c in q.where:
        if not (isinstance(c, Bind) and c.var in guards):
            elsewhere |= set(re.findall(r"[?$](\w+)", c.expr if isinstance(c, Bind) else c.text))
    for s, _, o in q.triples:
        elsewhere |= {t.name for t in (s, o) if isinstance(t, Var)}
    targets = q.bind_targets()
    if any(v in elsewhere or fallback in elsewhere or fallback in targets for v, (_, _, fallback) in guards.items()):
        return q, None
    triples, requires = [], []
    for s, p, o in q.triples:
        if isinstance(p, Var) and p.name in guards:
            predicate, required, _ = guards[p.name]
            triples.append((s, IRI(predicate), o))
            requires.append(required)
        else:
            triples.append((s, p, o))
            requires.append(frozenset())
    where = [c for c in q.where if not (isinstance(c, Bind) and c.var in guards)]
    return replace(q, triples=triples, where=where), requires


def analyse(q: TarqlQuery) -> QueryInfo:
    q, requires = recover_guards(q)
    binds = q.bind_targets()
    raw_vars: set[str] = set()
    for c in q.where:
        if isinstance(c, Raw):
            raw_vars |= {v for v in re.findall(r"[?$](\w+)", c.text)}
    def var_type(v: str, seen: frozenset = frozenset()):
        if v in binds:
            if v in seen:
                return None
            return infer_expr_type(binds[v], q.prefixes, lambda w: var_type(w, seen | {v}))
        if v == "ROWNUM":
            return XSD + "integer"
        if v in raw_vars:
            return None
        return XSD + "string"  # an unprocessed CSV column

    types: dict[str, object] = {v: var_type(v) for v in q.template_vars()}
    for v in sorted(frozenset().union(*requires) - set(types) if requires else ()):
        types[v] = var_type(v)  # required by a guard, but in no triple
    nonblank: set[str] = set()
    for s, p, o in q.triples:
        for t in (s, p):
            if isinstance(t, Var):
                types[t.name] = OTTR_IRI
        if isinstance(p, Var):
            nonblank.add(p.name)

    groups: dict = {}
    for s, p, o in q.triples:
        lst = groups.setdefault(s, [])
        if (p, o) not in lst:
            lst.append((p, o))
    occurrences: dict[str, set] = {}
    for s, items in groups.items():
        for t in [s] + [x for it in items for x in it]:
            if isinstance(t, BNode):
                occurrences.setdefault(t.label, set()).add(s)
    shared = {b for b, subs in occurrences.items() if len(subs) > 1}
    return QueryInfo(q, types, list(groups.items()), shared, nonblank, requires)


def canonical_shape(info: QueryInfo, subj, items) -> tuple[Shape, list]:
    """Return the shape of one subject group and the actual term for each parameter index."""

    def is_param(t):
        return isinstance(t, Var) or (isinstance(t, BNode) and t.label in info.shared_bnodes)

    def ptype(t):  # (type, non-blank)
        if isinstance(t, Var):
            return (info.types.get(t.name), t.name in info.nonblank)
        return (OTTR_IRI, False)

    def slot0(t):
        if t == subj:
            return ("S",)
        if is_param(t):
            return ("P", str(ptype(t)))
        if isinstance(t, BNode):
            return ("B",)
        return ("C", _tkey(t))

    ordered = sorted(items, key=lambda it: (slot0(it[0]), slot0(it[1])))
    args: list = []
    types: list = []
    hints: list[str] = []
    bnodes: list = []

    if is_param(subj):
        args.append(subj)
        types.append(ptype(subj))
        subject = ("P", str(types[0]))
    elif isinstance(subj, BNode):
        bnodes.append(subj)
        subject = ("B",)
    else:
        subject = ("C", _tkey(subj))

    def slot(t, role, pred):
        if t == subj:
            return ("S",)
        if is_param(t):
            if t not in args:
                args.append(t)
                types.append(ptype(t))
                hints.append(_hint(role, pred, t))
            return ("P", args.index(t))
        if isinstance(t, BNode):
            if t not in bnodes:
                bnodes.append(t)
            return ("B", bnodes.index(t))
        return ("C", t)

    canon = []
    for p, o in ordered:
        canon.append((slot(p, "predicate", None), slot(o, "object", p)))
    if subject[0] == "P":
        hints.insert(0, _subject_hint(subj, items))  # the order _template_base_name uses
    key_items = tuple((_k(a), _k(b)) for a, b in canon)
    key = (subject, key_items, tuple(str(t) for t in types))
    return Shape(key, subject, canon, types, hints), args


def _k(slot):
    return (slot[0], _tkey(slot[1])) if slot[0] == "C" else slot


def _hint(role: str, pred, t) -> str:
    if role == "predicate":
        return "predicate"
    if isinstance(pred, IRI):
        return "type" if pred.value == RDF_TYPE else safe_name(local_name(pred.value))
    return t.name if isinstance(t, Var) else "node"


def _subject_hint(subj, items) -> str:
    cls = _class_of(items)
    if cls:
        n = local_name(cls)
        return safe_name(n[:1].lower() + n[1:])
    return "subject"


def _class_of(items) -> str | None:
    for p, o in items:
        if isinstance(p, IRI) and p.value == RDF_TYPE and isinstance(o, IRI):
            return o.value
    return None


def _camel(s: str) -> str:
    s = re.sub(r"_?(iri|uri)$", "", s, flags=re.I) or s
    parts = [p for p in re.split(r"[^0-9A-Za-z]+", s) if p]
    out = "".join(p[:1].upper() + p[1:] for p in parts) or "Shape"
    return out if out[0].isalpha() else "T" + out


def _template_base_name(subj, items) -> str:
    cls = _class_of(items)
    if cls:
        return _camel(local_name(cls))
    if isinstance(subj, IRI):
        return _camel(local_name(subj.value))
    if isinstance(subj, Var):
        return _camel(subj.name)
    p = items[0][0]
    return _camel(local_name(p.value)) + "Node" if isinstance(p, IRI) else "Node"


# ---------------------------------------------------------------------------- factoring


def _embed(a: Shape, b: Shape):
    """Find a mapping of a's items onto distinct items of b, or None."""
    if a.subject != b.subject or a.subject[0] == "B" or a.has_local_bnodes():
        return None
    if a.subject[0] == "C" and a.subject != b.subject:
        return None
    a_items, b_items = a.items, b.items
    start = {0: 0} if a.subject_is_param else {}

    def match(sa, sb, m):
        if sa[0] != sb[0]:
            return None
        if sa[0] == "S":
            return m
        if sa[0] == "C":
            return m if _tkey(sa[1]) == _tkey(sb[1]) else None
        if sa[0] == "P":
            if a.types[sa[1]] != b.types[sb[1]]:
                return None
            if sa[1] in m:
                return m if m[sa[1]] == sb[1] else None
            if sb[1] in m.values():
                return None
            return {**m, sa[1]: sb[1]}
        return None

    def search(i, m, used):
        if i == len(a_items):
            return m, used
        pa, oa = a_items[i]
        for j, (pb, ob) in enumerate(b_items):
            if j in used:
                continue
            m1 = match(pa, pb, m)
            m2 = match(oa, ob, m1) if m1 is not None else None
            if m2 is not None:
                r = search(i + 1, m2, used | {j})
                if r:
                    return r
        return None

    return search(0, start, frozenset())


def factor_shapes(shapes: list[Shape]) -> None:
    for b in shapes:
        best = None
        for a in shapes:
            if a is b or len(a.items) >= len(b.items):
                continue
            r = _embed(a, b)
            if r and (best is None or len(a.items) > len(best[0].items)):
                best = (a, r[0], set(r[1]))
        b.parent = best


# ---------------------------------------------------------------------------- guarded queries


@dataclass(eq=False)
class Component:
    """Triples of a guarded query that require the same variables and are connected by
    a subject or a blank node. Each component becomes one template."""

    triples: list[tuple]
    requires: frozenset
    parent: Component | None = None
    children: list[Component] = field(default_factory=list)
    args: list = field(default_factory=list)  # query terms the caller passes, in parameter order
    params: list[Param] = field(default_factory=list)
    created: list[str] = field(default_factory=list)  # blank nodes the template makes itself
    name: str = ""

    @property
    def items(self) -> list[tuple]:
        return self.triples


def _vars_of(triple) -> set[str]:
    return {t.name for t in triple if isinstance(t, Var)}


def _terms_of(c: Component) -> set:
    return {t for triple in c.triples for t in triple if isinstance(t, (Var, BNode))}


def _ancestors(c: Component | None) -> list:
    out = []
    while c is not None:
        out.append(c)
        c = c.parent
    return out + [None]  # None stands for the root template


def guarded_components(info: QueryInfo) -> list[Component]:
    """Cut a guarded query into nested components (see the module docstring)."""
    q = info.query
    needed = frozenset().union(*info.requires)
    # a triple requires what its guard required, plus those of its own variables a guard needs
    reqs = [r | (_vars_of(t) & needed) for t, r in zip(q.triples, info.requires)]

    up = list(range(len(q.triples)))

    def find(i: int) -> int:
        while up[i] != i:
            up[i] = up[up[i]]
            i = up[i]
        return i

    first: dict = {}
    for i, (s, _, o) in enumerate(q.triples):
        for key in [(reqs[i], "s", s)] + [(reqs[i], "b", t.label) for t in (s, o) if isinstance(t, BNode)]:
            if key in first:
                up[find(i)] = find(first[key])
            else:
                first[key] = i
    by_root: dict[int, Component] = {}
    for i, t in enumerate(q.triples):
        by_root.setdefault(find(i), Component([], reqs[i])).triples.append(t)
    comps = list(by_root.values())

    # A component goes inside another only when it has to: when it shares a blank node
    # with one whose requirements it extends, or when it requires variables it does not
    # use (an enclosing template must then require them, or they would be unused
    # parameters). Otherwise the root calls it, with all its requirements as parameters.
    for c in comps:
        unused = c.requires - {t.name for t in _terms_of(c) if isinstance(t, Var)}
        below = [p for p in comps if p.requires < c.requires and unused <= p.requires]
        bnodes = {t for t in _terms_of(c) if isinstance(t, BNode)}
        sharing = [p for p in below if bnodes & _terms_of(p)]
        if sharing:
            c.parent = max(sharing, key=lambda p: len(p.requires))
        elif unused and below:
            c.parent = min(below, key=lambda p: len(p.requires))
        if c.parent is not None:
            c.parent.children.append(c)

    # a blank node is made by the lowest component above every use (or by the root)
    uses: dict[str, list[Component]] = {}
    for c in comps:
        for t in _terms_of(c):
            if isinstance(t, BNode):
                uses.setdefault(t.label, []).append(c)
    for label, cs in uses.items():
        maker = next(a for a in _ancestors(cs[0]) if all(a in _ancestors(c) for c in cs[1:]))
        if maker is not None:
            maker.created.append(label)

    def wire(c: Component) -> None:
        for child in c.children:
            wire(child)
        terms: list = []

        def need(t) -> None:
            if (isinstance(t, Var) or (isinstance(t, BNode) and t.label not in c.created)) and t not in terms:
                terms.append(t)

        for triple in c.triples:
            for t in triple:
                need(t)
        for v in sorted(c.requires - (c.parent.requires if c.parent else frozenset())):
            need(Var(v))
        for child in c.children:
            for t in child.args:
                need(t)
        c.args = terms
        c.params = _component_params(c, info)

    for c in comps:
        if c.parent is None:
            wire(c)
    return comps


def _arg_hint(t, c: Component, info: QueryInfo) -> str:
    for s, p, o in info.query.triples:  # a node named after its class
        if s == t and p == IRI(RDF_TYPE) and isinstance(o, IRI):
            n = local_name(o.value)
            return safe_name(n[:1].lower() + n[1:])
    for s, p, o in c.triples:
        if o == t and isinstance(p, IRI):
            return "type" if p.value == RDF_TYPE else safe_name(local_name(p.value))
        if p == t:
            return "predicate"
    for child in c.children:  # passed on: use the name the child gives it
        if t in child.args:
            return child.params[child.args.index(t)].name
    if isinstance(t, Var):
        name = t.name
        while (shorter := re.sub(r"_(iri|typed|d\d+)$", "", name)) != name:  # compose's suffixes
            name = shorter
        return safe_name(name)
    return "node"


def _component_params(c: Component, info: QueryInfo) -> list[Param]:
    params, names = [], set()
    for t in c.args:
        hint = _arg_hint(t, c, info)
        name, k = hint, 1
        while name in names:
            k += 1
            name = f"{hint}{k}"
        names.add(name)
        if isinstance(t, Var):
            params.append(Param(name, info.types.get(t.name), optional=t.name not in c.requires,
                                nonblank=t.name in info.nonblank))
        else:  # a blank node made by an enclosing template
            params.append(Param(name, OTTR_IRI))
    return params


def _component_base_name(c: Component) -> str:
    for _, p, o in c.triples:
        if p == IRI(RDF_TYPE) and isinstance(o, IRI):
            return _camel(local_name(o.value))
    s = c.triples[0][0]
    return _template_base_name(s, [(p, o) for s2, p, o in c.triples if s2 == s])


def _component_template(c: Component) -> Template:
    sub: dict = {t: Var(p.name) for t, p in zip(c.args, c.params)}
    sub |= {BNode(label): BNode(f"b{i}") for i, label in enumerate(c.created)}
    body = [Instance(OTTR_TRIPLE, [sub.get(t, t) for t in triple]) for triple in c.triples]
    body += [Instance(child.name, [sub[t] for t in child.args]) for child in c.children]
    return Template(c.name, c.params, body)


# ---------------------------------------------------------------------------- decomposition


@dataclass
class Decomposition:
    library: list[Template]
    roots: list[Template]
    root_prefixes: dict[str, PrefixMap]  # root IRI -> prefixes to render it with
    library_prefixes: PrefixMap
    template_ns: str
    query_ns: str

    def write(self, outdir: str | Path) -> list[Path]:
        outdir = Path(outdir)
        (outdir / "queries").mkdir(parents=True, exist_ok=True)
        written = []
        lib = outdir / "templates.stottr"
        lib.write_text(
            render_document(self.library, self.library_prefixes, header="Shared templates decomposed from TARQL queries."),
            encoding="utf8",
        )
        written.append(lib)
        voc = outdir / "tq-vocabulary.stottr"
        pm = PrefixMap({"tq": TQ, "ottr": OTTR, "xsd": XSD})
        voc.write_text(
            render_document(vocabulary_templates(), pm, header="Signatures of the tq: annotations that carry TARQL-only parts of a query."),
            encoding="utf8",
        )
        written.append(voc)
        for r in self.roots:
            fn = outdir / "queries" / (local_name(r.iri) + ".stottr")
            fn.write_text(render_document([r], self.root_prefixes[r.iri]), encoding="utf8")
            written.append(fn)
        return written


def _unique_prefix(pm: PrefixMap, wanted: str, ns: str) -> str:
    for p, n in pm.items():
        if n == ns:
            return p
    name, i = wanted, 1
    while name in pm.map:
        i += 1
        name = f"{wanted}{i}"
    pm.add(name, ns)
    return name


def decompose(
    queries: list[TarqlQuery],
    template_ns: str = "http://example.org/ottr/template/",
    query_ns: str = "http://example.org/ottr/query/",
    factor: bool = True,
) -> Decomposition:
    infos = [analyse(q) for q in queries]
    shapes: dict[tuple, Shape] = {}
    order: list[Shape] = []
    base_names: dict[int, str] = {}
    calls: list[list[tuple[Shape | Component, list]]] = []
    components: list[Component] = []

    for info in infos:
        if info.requires is not None:  # made by compose: nest by requirements instead
            comps = guarded_components(info)
            for c in comps:
                base_names[id(c)] = _component_base_name(c)
            components += comps
            calls.append([(c, c.args) for c in comps if c.parent is None])
            continue
        qcalls = []
        for subj, items in info.groups:
            shape, args = canonical_shape(info, subj, items)
            existing = shapes.get(shape.key)
            if existing is None:
                base_names[id(shape)] = _template_base_name(subj, items)
                shape.params = _make_params(shape)
                shapes[shape.key] = shape
                order.append(shape)
                existing = shape
            qcalls.append((existing, args))
        calls.append(qcalls)

    # within a family of same-named shapes the smallest gets the bare name
    families: dict[str, list[Shape | Component]] = {}
    for sh in order + components:
        families.setdefault(base_names[id(sh)], []).append(sh)
    for base, members in families.items():
        for i, sh in enumerate(sorted(members, key=lambda m: len(m.items))):
            sh.name = template_ns + (base if i == 0 else f"{base}_{i + 1}")

    if factor:
        factor_shapes(order)

    library = [_shape_template(s) for s in order] + [_component_template(c) for c in components]

    lib_pm = PrefixMap({"ottr": OTTR, "rdf": RDF, "xsd": XSD})
    _unique_prefix(lib_pm, "tpl", template_ns)
    for q in queries:
        for p, ns in q.prefixes.items():
            if p not in lib_pm.map and ns not in lib_pm.map.values():
                lib_pm.add(p, ns)

    roots, root_pms = [], {}
    used_names: set[str] = set()
    for info, qcalls in zip(infos, calls):
        q = info.query
        rname = safe_name(q.name)
        while rname in used_names:
            rname += "_"
        used_names.add(rname)
        root = _root_template(query_ns + rname, info, qcalls)
        roots.append(root)
        pm = q.prefixes.copy()
        for want, ns in (("ottr", OTTR), ("tq", TQ), ("tpl", template_ns), ("q", query_ns), ("xsd", XSD), ("rdf", RDF)):
            _unique_prefix(pm, want, ns)
        root_pms[root.iri] = pm
    return Decomposition(library, roots, root_pms, lib_pm, template_ns, query_ns)


def _make_params(shape: Shape) -> list[Param]:
    params, seen = [], set()
    for i, ((t, nb), h) in enumerate(zip(shape.types, shape.hints)):
        name, k = h, 1
        while name in seen:
            k += 1
            name = f"{h}{k}"
        seen.add(name)
        subject = i == 0 and shape.subject_is_param
        params.append(Param(name, t, optional=not subject, nonblank=nb))
    return params


def _slot_term(slot, shape: Shape, subj_term):
    kind = slot[0]
    if kind == "S":
        return subj_term
    if kind == "P":
        return Var(shape.params[slot[1]].name)
    if kind == "B":
        return BNode(f"b{slot[1]}")
    return slot[1]


def _subject_term(shape: Shape):
    if shape.subject[0] == "P":
        return Var(shape.params[0].name)
    if shape.subject[0] == "B":
        return BNode("b0")
    kind, key = shape.subject[1][0], shape.subject[1]
    if kind == "I":
        return IRI(key[1])
    return Literal(key[1], key[2] or None, key[3] or None)


def _shape_template(shape: Shape) -> Template:
    subj = _subject_term(shape)
    body: list[Instance] = []
    covered: set[int] = set()
    if shape.parent:
        parent, mapping, covered = shape.parent
        args = [Var(shape.params[mapping[j]].name) for j in range(len(parent.params))]
        body.append(Instance(parent.name, args))
    for i, (sp, so) in enumerate(shape.items):
        if i in covered:
            continue
        body.append(Instance(OTTR_TRIPLE, [subj, _slot_term(sp, shape, subj), _slot_term(so, shape, subj)]))
    return Template(shape.name, shape.params, body)


def _root_template(iri: str, info: QueryInfo, qcalls) -> Template:
    q = info.query
    names = q.template_vars()
    if info.requires:
        names += sorted(frozenset().union(*info.requires) - set(names))
    params = [Param(v, info.types.get(v), optional=True, nonblank=v in info.nonblank) for v in names]
    body = []
    for shape, args in qcalls:
        terms = [Var(a.name) if isinstance(a, Var) else BNode(a.label) for a in args]
        body.append(Instance(shape.name, terms))
    anns = [Instance(TQ_SOURCE, [Literal(q.name + ".rq")])]
    if q.base:
        anns.append(Instance(TQ_BASE, [Literal(q.base)]))
    for p, ns in q.prefixes.items():
        anns.append(Instance(TQ_PREFIX, [Literal(p), Literal(ns)]))
    if q.dataset:
        anns.append(Instance(TQ_DATASET, [Literal(q.dataset)]))
    for c in q.where:
        if isinstance(c, Bind):
            anns.append(Instance(TQ_BIND, [Literal(c.var), Literal(c.expr)]))
        else:
            anns.append(Instance(TQ_WHERE, [Literal(c.text)]))
    if q.tail:
        anns.append(Instance(TQ_MODIFIERS, [Literal(q.tail)]))
    return Template(iri, params, body, anns)

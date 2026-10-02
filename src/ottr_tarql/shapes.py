"""OTTR templates -> SHACL shapes for the RDF the templates produce.

Each root template is expanded symbolically, the same way ``compose`` expands
it. That gives every triple pattern the template can emit, the variables that
must be bound for each pattern to be emitted, and the declared type of every
variable. The shapes are derived from those:

Targets
    A subject with a constant ``rdf:type C`` gets a shape with
    ``sh:targetClass C``. A blank node that is the object of another pattern is
    checked through that pattern (``sh:node``). Any other subject gets a shape
    with ``sh:targetSubjectsOf p`` for the predicate it most reliably has.

``sh:minCount 1``
    Given when, in *every* template that can make a node a focus node of the
    shape, some pattern with that predicate is emitted whenever the pattern that
    made the node a focus node (its *anchor*) is. That is, the pattern's
    variables are among the anchor's, or are always bound (e.g. via a default).

Values
    For each predicate, the union over every pattern with that predicate in every
    expansion, so that a node receiving triples from several templates is never
    wrongly rejected:

    * ``sh:datatype`` for literal types;
    * ``sh:nodeKind`` for ``ottr:IRI`` (``sh:IRI`` if non-blank, else
      ``sh:BlankNodeOrIRI``, because OTTR lets a blank node fill an
      ``ottr:IRI`` parameter);
    * ``sh:in`` for constants;
    * ``sh:class`` when the object's own type triple is guaranteed;
    * ``sh:node`` for blank nodes the template builds;
    * list members for list types.

    By default ``sh:datatype`` is the declared type exactly, which is what
    ``compose``, ``decompose`` and oxi-gen produce. OTTR also accepts subtypes
    (``xsd:int`` for ``xsd:integer``); ``allow_subtypes=True`` admits them, using
    rOTTR's type hierarchy.

``sh:maxCount``
    Only with ``max_counts=True``. It assumes each node is built by a single
    instance (one CSV row), which is common but not something the templates say.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import rdflib
from rdflib.collection import Collection

from .compose import Pattern, _Expander, roots_in
from .ottr import IRI_TYPES, Library, ListType, LubType
from .terms import IRI, OTTR, OTTR_IRI, RDF, RDF_TYPE, RDFS, TQ, XSD, BNode, Literal, Var, local_name, safe_name

SH = rdflib.Namespace("http://www.w3.org/ns/shacl#")
OWL = "http://www.w3.org/2002/07/owl#"
DEFAULT_SHAPES_NS = "http://example.org/shapes/"

# rOTTR 0.2, table 1: basic type -> its supertype
SUPERTYPE = {
    OTTR_IRI: RDFS + "Resource",
    OWL + "Class": OTTR_IRI,
    OWL + "NamedIndividual": OTTR_IRI,
    RDFS + "Datatype": OTTR_IRI,
    OWL + "ObjectProperty": OTTR_IRI,
    OWL + "DatatypeProperty": OTTR_IRI,
    OWL + "AnnotationProperty": OTTR_IRI,
    RDFS + "Literal": RDFS + "Resource",
    OTTR + "string": RDFS + "Literal",
    RDF + "langString": OTTR + "string",
    XSD + "string": OTTR + "string",
    XSD + "normalizedString": XSD + "string",
    XSD + "token": XSD + "normalizedString",
    XSD + "language": XSD + "token",
    XSD + "Name": XSD + "token",
    XSD + "NCName": XSD + "Name",
    XSD + "NMTOKEN": XSD + "Name",
    OWL + "real": RDFS + "Literal",
    OWL + "rational": OWL + "real",
    XSD + "decimal": OWL + "rational",
    XSD + "integer": XSD + "decimal",
    XSD + "long": XSD + "integer",
    XSD + "int": XSD + "long",
    XSD + "short": XSD + "int",
    XSD + "byte": XSD + "short",
    XSD + "nonNegativeInteger": XSD + "integer",
    XSD + "positiveInteger": XSD + "nonNegativeInteger",
    XSD + "unsignedLong": XSD + "positiveInteger",
    XSD + "unsignedInt": XSD + "unsignedLong",
    XSD + "unsignedShort": XSD + "unsignedInt",
    XSD + "unsignedByte": XSD + "unsignedShort",
    XSD + "nonPositiveInteger": XSD + "integer",
    XSD + "negativeInteger": XSD + "nonPositiveInteger",
    XSD + "double": RDFS + "Literal",
    XSD + "float": RDFS + "Literal",
    XSD + "date": RDFS + "Literal",
    XSD + "dateTime": RDFS + "Literal",
    XSD + "dateTimeStamp": XSD + "dateTime",
    XSD + "time": RDFS + "Literal",
    XSD + "gYear": RDFS + "Literal",
    XSD + "gMonth": RDFS + "Literal",
    XSD + "gDay": RDFS + "Literal",
    XSD + "gYearMonth": RDFS + "Literal",
    XSD + "gMonthDay": RDFS + "Literal",
    XSD + "duration": RDFS + "Literal",
    XSD + "yearMonthDuration": XSD + "duration",
    XSD + "dayTimeDuration": XSD + "duration",
    XSD + "hexBinary": RDFS + "Literal",
    XSD + "base64Binary": RDFS + "Literal",
    XSD + "boolean": RDFS + "Literal",
    XSD + "anyURI": RDFS + "Literal",
    RDF + "HTML": RDFS + "Literal",
    RDF + "XMLLiteral": RDFS + "Literal",
}
ABSTRACT = {OTTR + "string", RDFS + "Literal", RDFS + "Resource"}  # no literal carries these as its datatype


def is_subtype(a, b) -> bool:
    if isinstance(a, LubType):
        a = a.inner
    if isinstance(b, LubType):
        b = b.inner
    if isinstance(a, ListType) or isinstance(b, ListType):
        return (
            isinstance(a, ListType)
            and isinstance(b, ListType)
            and (a.nonempty or not b.nonempty)
            and is_subtype(a.inner, b.inner)
        )
    if b is None or b == RDFS + "Resource":
        return True
    if b == OTTR_IRI and a in IRI_TYPES:
        return True
    while a is not None:
        if a == b:
            return True
        a = SUPERTYPE.get(a)
    return False


def most_specific(types: list):
    """The narrowest of the types a variable was declared with along the call chain."""
    cands = [t for t in types if t is not None and t != RDFS + "Resource"]
    for t in cands:
        if all(is_subtype(t, c) for c in cands):
            return t
    return cands[0] if cands else None


def subtypes_of(t: str) -> list[str]:
    return sorted(x for x in set(SUPERTYPE) | {t} if x not in ABSTRACT and is_subtype(x, t))


# ---------------------------------------------------------------------------- specification


@dataclass(frozen=True)
class Desc:
    """What one value of a property may look like. ``Desc()`` allows anything."""

    kind: str | None = None  # IRI | BlankNode | BlankNodeOrIRI | Literal
    datatypes: tuple = ()  # any of these
    classes: tuple = ()  # all of these
    node: "NodeSpec | None" = None
    values: tuple = ()  # one of these constants
    members: "Desc | None" = None  # for an RDF list: what each member may look like


@dataclass(frozen=True)
class PropSpec:
    path: str
    required: bool
    max_count: int | None
    has_values: tuple
    alternatives: tuple  # Desc-s, any of which a value must match; () = unconstrained


@dataclass(frozen=True)
class NodeSpec:
    props: tuple
    hint: str = field(default="Node", compare=False)


@dataclass
class _Expansion:
    template: str
    patterns: list[Pattern]
    types: dict
    nonblank: set
    always: set
    groups: dict = field(default_factory=dict)

    def __post_init__(self):
        for pat in self.patterns:
            self.groups.setdefault(pat.s, []).append(pat)

    def guaranteed(self, pat: Pattern, anchor: Pattern) -> bool:
        return pat.cond <= anchor.cond | self.always

    def type_patterns(self, subject) -> list[Pattern]:
        return [q for q in self.groups.get(subject, []) if q.p == IRI(RDF_TYPE) and isinstance(q.o, IRI)]

    def links_to(self, node) -> list[Pattern]:
        return [q for q in self.patterns if q.o == node]


def expand_symbolic(lib: Library, template: str) -> _Expansion:
    t = lib.get(lib.resolve(template))
    exp = _Expander(lib, lib.prefixes, symbolic_lists=True)
    exp.apply(t.iri, [Var(p.name) for p in t.params], frozenset(), 0)
    types = {v: most_specific(ts) for v, ts in exp.var_types.items()}
    return _Expansion(t.iri, exp.patterns, types, set(exp.var_nonblank), set(exp.always_bound))


def default_roots(lib: Library) -> list[str]:
    """The roots made by decompose if there are any, else every template with a pattern."""
    return roots_in(lib) or [iri for iri, t in lib.templates.items() if t.kind == "template" and t.body]


# ---------------------------------------------------------------------------- generation


class _Generator:
    def __init__(self, exps: list[_Expansion], allow_subtypes: bool, max_counts: bool):
        self.exps, self.allow_subtypes, self.max_counts = exps, allow_subtypes, max_counts
        self._global: dict[str, tuple] = {}

    # -- describing values ---------------------------------------------------------
    def type_desc(self, t, nonblank: bool) -> Desc:
        if t is None or t == RDFS + "Resource":
            return Desc()
        if isinstance(t, LubType):
            t = t.inner
        if isinstance(t, ListType):
            return Desc(kind="BlankNodeOrIRI", members=self.type_desc(t.inner, False))
        if t in IRI_TYPES or is_subtype(t, OTTR_IRI):
            return Desc(kind="IRI" if nonblank else "BlankNodeOrIRI")
        if t == RDFS + "Literal":
            return Desc(kind="Literal")
        if is_subtype(t, RDFS + "Literal"):
            dts = subtypes_of(t) if self.allow_subtypes or t in ABSTRACT else [t]
            return Desc(datatypes=tuple(dts))
        return Desc()  # a type outside rOTTR's table: nothing to say

    def describe(self, e: _Expansion, pat: Pattern, stack: frozenset = frozenset()) -> Desc:
        o = pat.o
        classes = tuple(sorted({tp.o.value for tp in e.type_patterns(o) if e.guaranteed(tp, pat)}))
        if isinstance(o, Var):
            d = self.type_desc(e.types.get(o.name), o.name in e.nonblank)
            return replace(d, classes=classes) if classes else d
        if isinstance(o, BNode):
            if o not in e.groups or o in stack:
                return Desc(kind="BlankNode")
            if e.type_patterns(o):  # checked by its class's shape
                return Desc(kind="BlankNode", classes=classes)
            return Desc(kind="BlankNode", node=self.nested(e, o, stack | {o}, pat.p))
        return Desc(values=(o,))

    def merge(self, descs) -> tuple:
        descs = set(descs)
        if not descs or Desc() in descs:
            return ()
        consts = [v for d in descs if d == Desc(values=d.values) for v in d.values]
        others = sorted((d for d in descs if d != Desc(values=d.values)), key=repr)
        consts = sorted(set(consts), key=_term_key)
        return tuple(([Desc(values=tuple(consts))] if consts else []) + others)

    def global_values(self, pred: str) -> tuple:
        """Every value any expansion can give ``pred``, so no producer is excluded."""
        if pred not in self._global:
            self._global[pred] = self.merge(
                self.describe(e, q) for e in self.exps for q in e.patterns if q.p == IRI(pred)
            )
        return self._global[pred]

    # -- shapes ----------------------------------------------------------------------
    def props(self, members, values) -> tuple:
        """members: (expansion, subject, anchors) for every way a focus node can arise."""
        preds = sorted({q.p.value for e, s, _ in members for q in e.groups[s] if isinstance(q.p, IRI)})
        out = []
        for pred in preds:
            p = IRI(pred)
            required, constants, max_count = True, None, 0
            for e, s, anchors in members:
                qs = [q for q in e.groups[s] if q.p == p]
                sure = [q for q in qs if all(e.guaranteed(q, a) for a in anchors)]
                required &= bool(sure)
                found = {q.o for q in sure if isinstance(q.o, (IRI, Literal))}
                constants = found if constants is None else constants & found
                if max_count is not None:
                    max_count = None if any(q.repeated for q in qs) else max(max_count, len({q.o for q in qs}))
            out.append(
                PropSpec(
                    pred,
                    required,
                    max_count if self.max_counts else None,
                    tuple(sorted(constants or (), key=_term_key)),
                    # types are often added by other sources or inference: only say which are guaranteed
                    () if pred == RDF_TYPE else values(pred),
                )
            )
        return tuple(out)

    def nested(self, e: _Expansion, node: BNode, stack: frozenset, via) -> NodeSpec:
        anchors = e.links_to(node)
        members = [(e, node, anchors)]

        def local(pred):
            return self.merge(self.describe(e, q, stack) for q in e.groups[node] if q.p == IRI(pred))

        hint = local_name(via.value) if isinstance(via, IRI) else "Node"
        return NodeSpec(self.props(members, local), hint=hint)

    def targets(self) -> dict:
        targets: dict = {}
        for e in self.exps:
            for s, pats in e.groups.items():
                tps = e.type_patterns(s)
                for cls in {tp.o.value for tp in tps}:
                    targets.setdefault(("class", cls), []).append((e, s, [tp for tp in tps if tp.o.value == cls]))
                if tps or (isinstance(s, BNode) and e.links_to(s)):
                    continue
                cands = [q for q in pats if isinstance(q.p, IRI)]
                if cands:
                    anchor = min(cands, key=lambda q: (len(q.cond), q.p.value))
                    targets.setdefault(("subjectsOf", anchor.p.value), [])
        # every subject that can have the predicate can be a focus node of a subjectsOf shape
        for key, members in targets.items():
            if key[0] != "subjectsOf":
                continue
            p = IRI(key[1])
            for e in self.exps:
                for s, pats in e.groups.items():
                    anchors = [q for q in pats if q.p == p]
                    if anchors:
                        members.append((e, s, anchors))
        return targets


def _term_key(t) -> tuple:
    if isinstance(t, IRI):
        return (0, t.value, "", "")
    return (1, t.lexical, t.datatype or "", t.lang or "")


# ---------------------------------------------------------------------------- RDF output


class _Writer:
    def __init__(self, shapes_ns: str, lib: Library):
        self.g = rdflib.Graph()
        self.ns = shapes_ns
        for p, ns in lib.prefixes.items():
            if ns not in (OTTR, TQ):
                self.g.bind(p, ns, override=False)
        for p, ns in (("sh", str(SH)), ("rdf", RDF), ("rdfs", RDFS), ("xsd", XSD), ("shape", shapes_ns)):
            self.g.bind(p, ns, override=False)
        self.names: set[str] = set()
        self.nested_iris: dict[NodeSpec, rdflib.URIRef] = {}

    def name(self, base: str) -> rdflib.URIRef:
        base = safe_name(base)
        n, i = base, 1
        while n in self.names:
            i += 1
            n = f"{base}{i}"
        self.names.add(n)
        return rdflib.URIRef(self.ns + n)

    def target_shape(self, targets: list[tuple[str, str]], props: tuple, templates: list[str]) -> None:
        """One shape for every target whose constraints are identical (same meaning, fewer duplicate reports)."""
        g = self.g
        # the target classes need no rdf:type check of their own; drop what is left empty
        classes = {IRI(iri) for kind, iri in targets if kind == "class"}
        kept = []
        for ps in props:
            if ps.path == RDF_TYPE and classes:
                ps = replace(ps, has_values=tuple(v for v in ps.has_values if v not in classes))
                if not ps.has_values and not ps.alternatives:
                    continue
            kept.append(ps)
        props = tuple(kept)
        kind, iri = targets[0]
        base = local_name(iri)
        shape = self.name(base[:1].upper() + base[1:] + ("Shape" if kind == "class" else "SubjectShape"))
        g.add((shape, rdflib.RDF.type, SH.NodeShape))
        what = []
        for kind, iri in targets:
            if kind == "class":
                g.add((shape, SH.targetClass, rdflib.URIRef(iri)))
                what.append(f"nodes of class <{iri}>")
            else:
                g.add((shape, SH.targetSubjectsOf, rdflib.URIRef(iri)))
                what.append(f"subjects of <{iri}>")
        srcs = ", ".join(f"<{t}>" for t in templates)
        comment = "; ".join(what)
        g.add((shape, rdflib.RDFS.comment, rdflib.Literal(f"{comment[:1].upper()}{comment[1:]}, as produced by {srcs}.")))
        self.write_props(shape, props)

    def write_props(self, shape, props: tuple) -> None:
        g = self.g
        for ps in props:
            node = rdflib.BNode()
            g.add((shape, SH.property, node))
            g.add((node, SH.path, rdflib.URIRef(ps.path)))
            if ps.required:
                g.add((node, SH.minCount, rdflib.Literal(1)))
            if ps.max_count is not None:
                g.add((node, SH.maxCount, rdflib.Literal(ps.max_count)))
            for v in ps.has_values:
                g.add((node, SH.hasValue, _to_rdf(v)))
            self.write_alternatives(node, ps.alternatives)

    def write_alternatives(self, node, alternatives: tuple) -> None:
        if len(alternatives) == 1:
            self.write_desc(node, alternatives[0])
        elif alternatives:
            items = []
            for d in alternatives:
                item = rdflib.BNode()
                self.write_desc(item, d)
                items.append(item)
            self.list(node, SH["or"], items)

    def write_desc(self, node, d: Desc) -> None:
        g = self.g
        if d.kind:
            g.add((node, SH.nodeKind, SH[d.kind]))
        if len(d.datatypes) == 1:
            g.add((node, SH.datatype, rdflib.URIRef(d.datatypes[0])))
        elif d.datatypes:
            items = []
            for dt in d.datatypes:
                item = rdflib.BNode()
                g.add((item, SH.datatype, rdflib.URIRef(dt)))
                items.append(item)
            self.list(node, SH["or"], items)
        for c in d.classes:
            g.add((node, SH["class"], rdflib.URIRef(c)))
        if d.values:
            self.list(node, SH["in"], [_to_rdf(v) for v in d.values])
        if d.node is not None:
            g.add((node, SH.node, self.nested(d.node)))
        if d.members is not None and d.members != Desc():
            inner, prop, step = rdflib.BNode(), rdflib.BNode(), rdflib.BNode()
            g.add((node, SH.node, inner))
            g.add((inner, SH.property, prop))
            g.add((step, SH.zeroOrMorePath, rdflib.RDF.rest))
            self.list(prop, SH.path, [step, rdflib.RDF.first])
            self.write_desc(prop, d.members)

    def nested(self, spec: NodeSpec) -> rdflib.URIRef:
        if spec not in self.nested_iris:
            shape = self.name(spec.hint[:1].upper() + spec.hint[1:] + "ValueShape")
            self.nested_iris[spec] = shape
            self.g.add((shape, rdflib.RDF.type, SH.NodeShape))
            self.write_props(shape, spec.props)
        return self.nested_iris[spec]

    def list(self, node, pred, items: list) -> None:
        head = rdflib.BNode()
        Collection(self.g, head, items)
        self.g.add((node, pred, head))


def _to_rdf(t):
    if isinstance(t, IRI):
        return rdflib.URIRef(t.value)
    if t.lang:
        return rdflib.Literal(t.lexical, lang=t.lang)
    return rdflib.Literal(t.lexical, datatype=rdflib.URIRef(t.datatype) if t.datatype else None)


def generate_shapes(
    lib: Library,
    templates: list[str] | None = None,
    shapes_ns: str = DEFAULT_SHAPES_NS,
    max_counts: bool = False,
    allow_subtypes: bool = False,
) -> rdflib.Graph:
    """SHACL shapes for the RDF that instances of ``templates`` can produce.

    ``templates`` defaults to the roots made by ``decompose`` if the library has
    any, and otherwise to every template (the shapes then hold for any of them).
    """
    roots = [lib.resolve(t) for t in templates] if templates else default_roots(lib)
    exps = [expand_symbolic(lib, r) for r in roots]
    gen = _Generator(exps, allow_subtypes, max_counts)
    shapes: dict[tuple, tuple[list, set]] = {}  # props -> (targets, templates)
    for (kind, iri), members in sorted(gen.targets().items()):
        if not members:
            continue
        props = gen.props(members, gen.global_values)
        targets, templates_used = shapes.setdefault(props, ([], set()))
        targets.append((kind, iri))
        templates_used.update(e.template for e, _, _ in members)
    out = _Writer(shapes_ns, lib)
    for props, (targets, templates_used) in shapes.items():
        out.target_shape(targets, props, sorted(templates_used))
    return out.g

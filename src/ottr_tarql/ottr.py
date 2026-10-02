"""OTTR template model with a stOTTR parser and serializer (stOTTR 0.1.x)."""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urljoin

from .lexer import Stream, SyntaxErr, tokenize
from .terms import (
    IRI,
    NONE,
    OTTR,
    OTTR_IRI,
    OTTR_TRIPLE,
    RDFS,
    XSD,
    BNode,
    ListTerm,
    Literal,
    PrefixMap,
    Var,
    render_iri,
    render_term,
)

# ---------------------------------------------------------------------------- types


@dataclass(frozen=True)
class ListType:
    inner: "Type"
    nonempty: bool = False


@dataclass(frozen=True)
class LubType:
    inner: str


Type = str | ListType | LubType  # a basic type is its IRI


def render_type(t: Type, pm: PrefixMap) -> str:
    if isinstance(t, ListType):
        return ("NEList<" if t.nonempty else "List<") + render_type(t.inner, pm) + ">"
    if isinstance(t, LubType):
        return "LUB<" + render_type(t.inner, pm) + ">"
    c = pm.compact(t)
    if c is None:
        raise ValueError(f"stOTTR requires a prefixed name for type <{t}>; declare a prefix for it")
    return c


# OWL/RDF vocabulary types that are subtypes of ottr:IRI (rOTTR, table 1)
IRI_TYPES = {
    OTTR_IRI,
    OTTR + "IRI",
    RDFS + "Class",
    RDFS + "Datatype",
    "http://www.w3.org/1999/02/22-rdf-syntax-ns#Property",
    "http://www.w3.org/2002/07/owl#Class",
    "http://www.w3.org/2002/07/owl#NamedIndividual",
    "http://www.w3.org/2002/07/owl#ObjectProperty",
    "http://www.w3.org/2002/07/owl#DatatypeProperty",
    "http://www.w3.org/2002/07/owl#AnnotationProperty",
    "http://www.w3.org/2002/07/owl#Ontology",
}
TOP_TYPES = {None, RDFS + "Resource", RDFS + "Literal", XSD + "string", "http://www.w3.org/1999/02/22-rdf-syntax-ns#langString"}

# ---------------------------------------------------------------------------- model


@dataclass
class Param:
    name: str
    type: Type | None = None
    optional: bool = False
    nonblank: bool = False
    default: object = None  # constant term or None


@dataclass
class Instance:
    template: str
    args: list
    expand_flags: list[bool] = field(default_factory=list)
    expander: str | None = None  # cross | zipMin | zipMax

    def __post_init__(self):
        if not self.expand_flags:
            self.expand_flags = [False] * len(self.args)


@dataclass
class Template:
    iri: str
    params: list[Param]
    body: list[Instance] | None = None  # None for BASE templates and bare signatures
    annotations: list[Instance] = field(default_factory=list)
    kind: str = "template"  # template | base | signature

    def param(self, name: str) -> Param | None:
        return next((p for p in self.params if p.name == name), None)

    def annotations_of(self, iri: str) -> list[Instance]:
        return [a for a in self.annotations if a.template == iri]


TRIPLE = Template(
    OTTR_TRIPLE,
    [Param("subject", OTTR_IRI), Param("predicate", OTTR_IRI, nonblank=True), Param("object", RDFS + "Resource")],
    kind="base",
)


@dataclass
class Document:
    prefixes: PrefixMap
    templates: dict[str, Template] = field(default_factory=dict)
    instances: list[Instance] = field(default_factory=list)


class Library:
    """A set of templates gathered from one or more stOTTR documents."""

    def __init__(self, docs: list[Document] | None = None):
        self.templates: dict[str, Template] = {OTTR_TRIPLE: TRIPLE}
        self.prefixes = PrefixMap()
        for d in docs or []:
            self.add(d)

    def add(self, doc: Document) -> None:
        for p, ns in doc.prefixes.items():
            self.prefixes.add(p, ns, override=False)
        for iri, t in doc.templates.items():
            prev = self.templates.get(iri)
            if prev is not None and prev.kind == "template" and t.kind != "template":
                continue  # keep the definition over a later bare signature
            self.templates[iri] = t

    def get(self, iri: str) -> Template:
        if iri not in self.templates:
            raise KeyError(f"template <{iri}> is not defined in the library")
        return self.templates[iri]

    def resolve(self, name: str) -> str:
        """Accept a full IRI, <IRI> or a prefixed name."""
        name = name.strip("<>")
        if name in self.templates:
            return name
        if ":" in name:
            p, _, local = name.partition(":")
            if p in self.prefixes.map:
                return self.prefixes.expand(p, local)
        return name


# ---------------------------------------------------------------------------- parsing


def parse_stottr(text: str) -> Document:
    s = Stream(tokenize(text, "stottr"), text)
    doc = Document(PrefixMap())
    p = _Parser(s, doc)
    while not s.eof():
        p.statement()
    return doc


class _Parser:
    def __init__(self, s: Stream, doc: Document):
        self.s, self.doc, self.base, self.anon = s, doc, None, 0

    # directives and statements
    def statement(self) -> None:
        s = self.s
        if s.at("DIRECTIVE", "@prefix") or s.at("NAME", "PREFIX", ci=True):
            turtle = s.next().kind == "DIRECTIVE"
            pt = s.expect("PNAME")
            self.doc.prefixes.add(pt.prefix, self._resolve(s.expect("IRIREF").value))
            if turtle:
                s.expect("PUNCT", ".")
            return
        if s.at("DIRECTIVE", "@base") or s.at("NAME", "BASE", ci=True):
            turtle = s.next().kind == "DIRECTIVE"
            self.base = self._resolve(s.expect("IRIREF").value)
            if turtle:
                s.expect("PUNCT", ".")
            return
        if s.at("NAME") and s.peek().value in ("cross", "zipMin", "zipMax"):
            self.doc.instances.append(self.instance())
        else:
            name = self.iri()
            if s.at("PUNCT", "["):
                t = self.signature(name)
                if s.at("PUNCT", "::"):
                    s.next()
                    if s.at("NAME", "BASE"):
                        s.next()
                        t.kind = "base"
                    else:
                        t.body = self.pattern()
                        t.kind = "template"
                else:
                    t.kind = "signature"
                self.doc.templates[t.iri] = t
            else:
                self.doc.instances.append(self.instance(name))
        s.expect("PUNCT", ".")

    def signature(self, name: str) -> Template:
        s = self.s
        s.expect("PUNCT", "[")
        params: list[Param] = []
        while not s.at("PUNCT", "]"):
            params.append(self.param())
            if s.at("PUNCT", ","):
                s.next()
        s.expect("PUNCT", "]")
        anns: list[Instance] = []
        while s.at("PUNCT", "@@"):
            s.next()
            anns.append(self.instance())
            if s.at("PUNCT", ","):
                s.next()
        return Template(name, params, annotations=anns)

    def param(self) -> Param:
        s = self.s
        p = Param("")
        while s.at("PUNCT", "?") or s.at("PUNCT", "!"):
            if s.next().value == "?":
                p.optional = True
            else:
                p.nonblank = True
        if not s.at("VAR"):
            p.type = self.type()
        p.name = s.expect("VAR").value
        if s.at("PUNCT", "="):
            s.next()
            p.default = self.term()
        return p

    def type(self) -> Type:
        s = self.s
        if s.at("TYPEOPEN"):
            kw = s.next().value
            inner = self.type()
            s.expect("PUNCT", ">")
            if kw == "LUB<":
                return LubType(inner)
            return ListType(inner, nonempty=kw.startswith("NE"))
        return self.iri()

    def pattern(self) -> list[Instance]:
        s = self.s
        s.expect("PUNCT", "{")
        body: list[Instance] = []
        while not s.at("PUNCT", "}"):
            body.append(self.instance())
            if s.at("PUNCT", ","):
                s.next()
        s.expect("PUNCT", "}")
        return body

    def instance(self, name: str | None = None) -> Instance:
        s = self.s
        expander = None
        if name is None:
            if s.at("NAME") and s.peek().value in ("cross", "zipMin", "zipMax") and s.at("PUNCT", "|", k=1):
                expander = s.next().value
                s.next()
            name = self.iri()
        s.expect("PUNCT", "(")
        args, flags = [], []
        while not s.at("PUNCT", ")"):
            flag = False
            if s.at("PUNCT", "++"):
                s.next()
                flag = True
            args.append(self.term())
            flags.append(flag)
            if s.at("PUNCT", ","):
                s.next()
        s.expect("PUNCT", ")")
        return Instance(name, args, flags, expander)

    # terms
    def iri(self) -> str:
        t = self.s.next()
        if t.kind == "IRIREF":
            return self._resolve(t.value)
        if t.kind == "PNAME":
            try:
                return self.doc.prefixes.expand(t.prefix, t.local)
            except KeyError as e:
                raise SyntaxErr(str(e)) from None
        raise SyntaxErr(f"expected an IRI but found {t.value!r}")

    def term(self):
        s = self.s
        t = s.peek()
        if t is None:
            raise SyntaxErr("unexpected end of input")
        if t.kind in ("IRIREF", "PNAME"):
            return IRI(self.iri())
        s.next()
        if t.kind == "VAR":
            return Var(t.value)
        if t.kind == "BNODE":
            return BNode(t.value)
        if t.kind == "STRING":
            if s.at("LANGTAG"):
                return Literal(t.value, lang=s.next().value)
            if s.at("DTYPE"):
                s.next()
                return Literal(t.value, self.iri())
            return Literal(t.value)
        if t.kind == "NUMBER":
            return Literal(t.value, XSD + t.numtype)
        if t.kind == "NAME":
            if t.value in ("true", "false"):
                return Literal(t.value, XSD + "boolean")
            if t.value == "none":
                return NONE
        if t.kind == "PUNCT" and t.value == "[":
            s.expect("PUNCT", "]")
            self.anon += 1
            return BNode(f"anon{self.anon}")
        if t.kind == "PUNCT" and t.value == "(":
            items = []
            while not s.at("PUNCT", ")"):
                items.append(self.term())
                if s.at("PUNCT", ","):
                    s.next()
            s.expect("PUNCT", ")")
            return ListTerm(tuple(items))
        raise SyntaxErr(f"unexpected token {t.value!r}")

    def _resolve(self, iri: str) -> str:
        return urljoin(self.base, iri) if self.base and ":" not in iri.split("/")[0] else iri


# ---------------------------------------------------------------------------- serialization


def render_instance(i: Instance, pm: PrefixMap) -> str:
    args = ", ".join(("++" if f else "") + render_term(a, pm, "stottr") for a, f in zip(i.args, i.expand_flags))
    head = f"{i.expander} | " if i.expander else ""
    return f"{head}{render_iri(i.template, pm)}({args})"


def render_param(p: Param, pm: PrefixMap) -> str:
    mods = ("!" if p.nonblank else "") + ("?" if p.optional else "")
    parts = [mods] if mods else []
    if p.type is not None:
        parts.append(render_type(p.type, pm))
    parts.append("?" + p.name)
    out = " ".join(parts)
    if p.default is not None:
        out += " = " + render_term(p.default, pm, "stottr")
    return out


def render_template(t: Template, pm: PrefixMap) -> str:
    lines = [render_iri(t.iri, pm) + " ["]
    for i, p in enumerate(t.params):
        lines.append("    " + render_param(p, pm) + ("," if i < len(t.params) - 1 else ""))
    lines.append("]")
    if not t.params:
        lines = [render_iri(t.iri, pm) + " [ ]"]
    for i, a in enumerate(t.annotations):
        lines.append("  @@ " + render_instance(a, pm) + ("," if i < len(t.annotations) - 1 else ""))
    if t.kind == "base":
        lines.append(":: BASE .")
    elif t.kind == "signature":
        lines[-1] += " ."
    else:
        lines.append(":: {")
        body = t.body or []
        for i, inst in enumerate(body):
            lines.append("    " + render_instance(inst, pm) + ("," if i < len(body) - 1 else ""))
        lines.append("} .")
    return "\n".join(lines)


def render_document(templates: list[Template], pm: PrefixMap, instances: list[Instance] = (), header: str = "") -> str:
    out = []
    if header:
        out += ["# " + ln if ln else "#" for ln in header.splitlines()] + [""]
    out += [f"@prefix {p}: <{ns}> ." for p, ns in pm.items()]
    out.append("")
    for t in templates:
        out.append(render_template(t, pm))
        out.append("")
    for i in instances:
        out.append(render_instance(i, pm) + " .")
    return "\n".join(out).rstrip() + "\n"

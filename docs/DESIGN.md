# Design: converting between TARQL CONSTRUCT mappings and OTTR templates

This tool converts in both directions between two ways of describing how tabular data becomes RDF:

* **TARQL-style SPARQL CONSTRUCT** ([TARQL](https://tarql.github.io/), [oxi-gen](https://github.com/semanticarts/oxi-gen)).
  The query runs once per CSV row. Each column is bound to `?column`, `?ROWNUM` holds the row
  number, `BIND`s in the WHERE clause derive IRIs and typed values, and the CONSTRUCT template says
  which triples to produce.
* **OTTR templates** ([ottr.xyz](https://ottr.xyz), the [stOTTR](https://spec.ottr.xyz/stOTTR/0.1/)
  syntax, the [Lutra](https://gitlab.com/ottr/lutra/lutra) reference implementation). These are
  named, parameterised, nestable patterns that expand down to the base template `ottr:Triple`.

```
                 decompose                                  compose
 *.rq  ───────────────────────────►  templates.stottr   ───────────────►  *.rq
 (TARQL)                             queries/*.stottr                     (TARQL)
                                     tq-vocabulary.stottr
                                           │
              instances (CSV rows ─► stOTTR instances)  ─► Lutra expand ─► RDF
```

## 1. Why the two forms fit together

Both formalisms handle a missing value in a similar way, and the whole mapping depends on this.

| TARQL / SPARQL CONSTRUCT | OTTR |
|---|---|
| An empty CSV cell leaves its variable unbound. | The argument is `none`. |
| A template triple with an unbound variable is skipped. Other triples are still produced. | `ottr:Triple` has three **mandatory** parameters (rOTTR §7), so an instance with a `none` argument is dropped. |
| A blank node in the template is fresh for every row. | A blank node in a template body is fresh for every instance (rOTTR §6). |
| One solution per row. | One root-template instance per row. |
| `BIND(expr AS ?v)`, `FILTER`, string functions | No counterpart. OTTR has no functions. |

So the CONSTRUCT template maps onto OTTR's patterns, and the WHERE clause maps onto nothing in
OTTR. The design therefore splits a TARQL query into two layers:

1. **The shape layer, as real OTTR.** Templates whose parameters are the *post-BIND* variables
   (`?id_iri`, not `?id`). Lutra can lint, document and expand them, and you can reuse them by hand.
2. **The lifting layer, as annotations.** The WHERE clause, the prefixes, and the dataset and
   solution modifiers are stored verbatim as `tq:` annotation instances (`@@`) on the query's root
   template. Annotations are part of stOTTR, and expansion ignores them, so the templates stay valid
   OTTR while still carrying what `compose` needs to rebuild the query.

The prefixes go into annotations, not just into the stOTTR `@prefix` header, because
`tarql:expandPrefixedName(":x")` resolves against the *query's* prologue at run time. Two queries
in one decomposition can also bind the same prefix name to different namespaces.

## 2. TARQL → OTTR (`decompose`)

The input is a **set** of queries. Reuse across files is the point of this step.

1. **Parse.** The CONSTRUCT template is parsed into terms (Turtle shorthand: `a`, `;`, `,`,
   `[ … ]`, every literal form). The WHERE group is split into top-level `BIND(expr AS ?v)` clauses
   and opaque raw clauses (FILTER, OPTIONAL, VALUES, …). Both kinds are kept as source text, in
   their original order.
2. **Type the variables.** Each variable in the CONSTRUCT template gets an OTTR type:
   * `IRI()`/`URI()`/`tarql:expandPrefixedName()` → `ottr:IRI`; `xsd:T(...)` or `STRDT(_, xsd:T)` →
     `xsd:T`; `STRLANG` → `rdf:langString`; string functions → `xsd:string`;
   * a variable that is never a BIND target is a raw CSV column → `xsd:string`. `?ROWNUM` → `xsd:integer`;
   * a variable in subject or predicate position → `ottr:IRI`. A variable in predicate position is
     also **non-blank** (`!`), because `ottr:Triple`'s predicate is non-blank and Lutra checks that
     the flag is consistent along the call chain.
3. **Cut into shapes.** Triples are grouped by subject. Each group is a *shape*. A blank node used
   by more than one group is created in the root template and passed down as an argument. A blank
   node that appears in only one group stays inside that template.
4. **Canonicalise.** The items of a shape are sorted, and variables are replaced by numbered,
   typed slots. Two shapes with the same canonical key are the same template, even when they come
   from different files and use different variable names.
5. **Factor (on by default, `--no-factor` to disable).** If shape *A* embeds into shape *B* (same
   subject kind, a consistent and type-preserving mapping of variables, *A* has no local blank
   nodes), then *B*'s body becomes `A(...)` plus the remaining triples. The largest such *A* is
   chosen, so chains form naturally (`Item` ⊂ `Item_2` ⊂ …).
6. **Name.** Templates are named after their `rdf:type` class, or else the constant subject or the
   subject variable (`tpl:Item`, `tpl:Item_2`). Within a family the smallest shape gets the bare
   name. Parameters are named after their predicate (`?prefLabel`), and the subject parameter after
   its class (`?item`).
7. **Build a root template per query.** Its parameters are all the CONSTRUCT variables, all
   **optional**, because any cell may be empty. Its body is one instance per shape. Its annotations
   hold the parts that are not OTTR.

Mandatory vs optional parameters:

* *Root parameters are optional.* A row can lack anything.
* *A shape's subject parameter is mandatory.* When the subject is unbound, TARQL skips every triple
  of that subject, and OTTR drops the whole shape instance. The result is the same.
* *Object parameters are optional.* Only that one triple disappears. The `none` reaches
  `ottr:Triple`, which drops it.

### Worked example (oxi-gen `successor_field.rq` and `optional_field.rq`)

```sparql
construct { ?id_iri a :Item ; :prefLabel ?pref_label ; :altLabel ?alt_label ; :hasSuccessor ?successor_iri . }
where { BIND(tarql:expandPrefixedName(?id) AS ?id_iri)
        BIND(tarql:expandPrefixedName(?successor) AS ?successor_iri) }
```
becomes (the shared library: `optional_field.rq` contributed `tpl:Item`)
```stottr
tpl:Item [ ottr:IRI ?item, ? xsd:string ?altLabel, ? xsd:string ?prefLabel ] :: {
    ottr:Triple(?item, rdf:type, :Item),
    ottr:Triple(?item, :altLabel, ?altLabel),
    ottr:Triple(?item, :prefLabel, ?prefLabel) } .

tpl:Item_2 [ ottr:IRI ?item, ? xsd:string ?altLabel, ? ottr:IRI ?hasSuccessor, ? xsd:string ?prefLabel ] :: {
    tpl:Item(?item, ?altLabel, ?prefLabel),
    ottr:Triple(?item, :hasSuccessor, ?hasSuccessor) } .
```
and the root template
```stottr
q:successor_field [ ? ottr:IRI ?id_iri, ? xsd:string ?pref_label, ? xsd:string ?alt_label, ? ottr:IRI ?successor_iri ]
  @@ tq:Source("successor_field.rq"),
  @@ tq:Prefix("", "https://test.com/d/"),
  @@ tq:Bind("id_iri", "tarql:expandPrefixedName(?id)"),
  @@ tq:Bind("successor_iri", "tarql:expandPrefixedName(?successor)")
:: { tpl:Item_2(?id_iri, ?alt_label, ?successor_iri, ?pref_label) } .
```

### Annotation vocabulary (`tq:` = `http://example.org/ottr-tarql#`)

| Annotation | Carries |
|---|---|
| `tq:Source("file.rq")` | origin. Also marks the template as a root for `compose --all`. |
| `tq:Prefix("p", "ns")`, `tq:Base("iri")` | the query prologue |
| `tq:Bind("var", "expr")` | one `BIND(expr AS ?var)` |
| `tq:Where("text")` | any other WHERE clause, verbatim |
| `tq:Dataset("text")`, `tq:Modifiers("text")` | `FROM …`, and `LIMIT`/`ORDER BY` … |

Their signatures are written to `tq-vocabulary.stottr` so Lutra resolves them. The namespace is a
placeholder. Change `TQ` in `terms.py` if you mint a permanent one.

## 3. OTTR → TARQL (`compose`)

`compose` expands a template **symbolically**: its parameters become SPARQL variables, nested
instances are expanded recursively, and every resulting `ottr:Triple` becomes a CONSTRUCT triple.
Nested blank nodes get fresh labels for each instance. Constant list arguments become RDF
collections.

Where OTTR's semantics differ from "skip triples with unbound variables", the WHERE clause makes up
the difference:

| OTTR feature | Generated SPARQL |
|---|---|
| `none` or a constant passed in the template | resolved statically |
| default value `?x = d` with a variable argument | `BIND(COALESCE(?x, d) AS ?x_dN)`. A blank-node default becomes `BNODE()`. |
| mandatory parameter `?m`, triple that doesn't mention `?m` | the predicate is replaced by `?_gN`, with `BIND(IF(sameTerm(?m, ?m), pred, ?_unbound) AS ?_gN)`. `sameTerm` is used rather than `BOUND`, because oxi-gen substitutes CSV values into the query, and after that `BOUND(?column)` is always false. |
| `cross` / `zipMin` / `zipMax` over constant lists | unrolled at compose time |

The guard rule follows from OTTR: an instance with `none` for a mandatory parameter is removed
*entirely*. That includes its triples about blank nodes or constants, which TARQL would otherwise
still emit. A triple that already contains every required variable needs no guard, so round-tripped
queries come out unguarded and close to the original text.

**Where the WHERE clause comes from:**

* *A root made by `decompose`* (it has `tq:` annotations): the recorded prologue and clauses are
  restored verbatim. Generated COALESCE and guard BINDs are appended.
* *A hand-written template* (no annotations): each parameter is read from the CSV column of the
  same name, in the spirit of tabOTTR. Typed parameters are converted:
  * IRI types (`ottr:IRI`, `owl:Class`, …) → `IF(CONTAINS(STR(?c), "://"), IRI(?c), tarql:expandPrefixedName(?c))`,
    so the column may hold either full IRIs or prefixed names;
  * `xsd:T` (except `xsd:string`) → `STRDT(?c, xsd:T)`;
  * `--no-infer-binds` turns this off.

`compose` also returns the root-parameter → query-variable map. `instances` uses it to evaluate the
composed WHERE clause over a CSV and emit one stOTTR instance per row. Lutra can then expand those
instances, which is an independent check that both forms produce the same RDF.

## 4. How correctness is checked

`tests/test_roundtrip.py` checks these equivalences up to graph isomorphism, on every fixture and
its CSV (the oxi-gen test fixtures, plus extra ones covering blank nodes, shared shapes, every
literal form, predicate variables, FILTERs and oxi-gen's `BOUND` behaviour):

1. `run(q) ≅ run(compose(decompose(q)))`: the TARQL → OTTR → TARQL round trip.
2. `run(q) ≅ Lutra(instances(decompose(q), csv))`: the decomposed templates mean what the query
   meant, according to the reference OTTR implementation.
3. `Lutra(instances(T, csv)) ≅ run(compose(T))` for a hand-written library with defaults, mandatory
   parameters, nested blank nodes, `cross` and list objects.
4. Lutra `lint` reports no warnings or errors on the generated library.

`tests/test_oxigen.py` runs the same queries through the real oxi-gen binary when `OXI_GEN` points
to one. It checks that the emulation and every composed query agree with oxi-gen.
`tests/test_shapes.py` checks that the generated shapes (§5) accept the output for every fixture and
reject deliberately broken data. It uses SHACL_Engine (`pip install shacl`).

`run` is an rdflib emulation of oxi-gen. It reproduces oxi-gen's behaviour in these respects:

* one solution per row (supplied through `VALUES`);
* blank or whitespace cells are unbound;
* `\` escapes the next character inside quoted CSV fields;
* `?ROWNUM` counts from 0;
* `tarql:expandPrefix` and `tarql:expandPrefixedName` are available;
* `BOUND(?column)` is always false, because oxi-gen substitutes CSV values into the query.

## 5. SHACL shapes from templates (`shapes`)

`ottr-tarql shapes` turns templates into SHACL shapes describing the RDF the templates produce, so
output from oxi-gen, Lutra or any other source can be checked. For example, use SHACL_Engine
(`pip install shacl`) or `holos validate`. Each root template is expanded the same way `compose`
expands it. That gives every triple pattern, the variables each pattern needs, and the declared
type of every variable.

| In the templates | In the shapes |
|---|---|
| a subject with a constant `rdf:type C` | a shape with `sh:targetClass C` (classes with identical constraints share one shape) |
| a blank node built by a template and pointed to by another triple | `sh:node`, to a nested shape |
| any other subject | `sh:targetSubjectsOf p`, where `p` is the predicate it most reliably has |
| literal type `xsd:T` | `sh:datatype xsd:T`. With `--allow-subtypes`, any rOTTR subtype is also accepted, as OTTR's type checker would. |
| `ottr:IRI`, `owl:Class`, … | `sh:nodeKind sh:BlankNodeOrIRI`, or `sh:IRI` if the parameter is non-blank (`!`) |
| a constant object | `sh:in`, plus `sh:hasValue` if the triple is always present |
| an object whose own type triple always comes with it | `sh:class` |
| `List<T>` | each member checked through the path `( [ sh:zeroOrMorePath rdf:rest ] rdf:first )` |
| a triple always emitted with the node | `sh:minCount 1` |
| `--max-counts` | `sh:maxCount`, assuming one instance per node |

Two rules keep the shapes from rejecting valid output:

* **`sh:minCount`.** A triple is "always emitted with the node" when its variables are a subset of
  the variables of the triple that makes the node a focus node (its type triple or the linking
  triple), allowing for variables that are always bound, such as those with defaults. This must
  hold in **every** template that can produce such nodes.
  * By default, the roots made by `decompose` are used if there are any, and otherwise every
    template in the library.
  * `-T` limits this to the templates you actually instantiate, which gives tighter shapes.
* **Values.** The values allowed for a predicate are the union over every pattern with that
  predicate in every root, because one node can receive triples from several templates.
  `rdf:type` is not restricted, since other sources and inference often add types. Only
  guaranteed extra classes become `sh:hasValue`.

## 6. Limitations and extension points

| Not supported | Why / possible extension |
|---|---|
| RDF 1.2 reifiers `~`, annotations `{| |}`, triple terms `<< >>` (oxi-gen supports them) | OTTR has no triple terms. A future option could lower `s p o ~r` to `r rdf:reifies <<( s p o )>>` once OTTR gains triple terms, or to classic `rdf:Statement` reification. These queries are skipped with a message. |
| RDF collections `( … )` in a CONSTRUCT template | could map to OTTR list constants, since compose already emits lists as collections |
| list-typed parameters fed from data | A TARQL row has one value per column. The natural mapping is oxi-gen's `--split COL ITEM DELIM`: `cross \| T(++?col)` ↔ `--split col col_item ";"` with `?col_item` in the template. That flag lives on the command line, not in the query, so it is left for a future version. |
| `!` (non-blank) and type checks at run time | They become declarations only, and the SPARQL does not enforce them. Check the output with the shapes from §5 instead. |
| generalising constants | Shapes that differ only in a constant (`a :Item` vs `a :Product`) stay separate. Anti-unification could lift such constants to parameters. |
| common sub-shapes | Factoring reuses only shapes that already exist as whole groups. It does not mine shared sub-patterns. |
| bOTTR | The `tq:` annotations play the role of a bOTTR source mapping. Exporting them as bOTTR (H2 SQL over CSV) is possible, but would need SPARQL expressions translated to SQL. |

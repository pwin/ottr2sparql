# Using OTTR templates with oxi-gen

This guide is about how [oxi-gen](https://github.com/semanticarts/oxi-gen) could use
[OTTR](https://ottr.xyz) templates, and what each step would require. It starts with what runs
today with no change to oxi-gen, then moves up a ladder of changes, from a few lines of Rust to
a native template engine.

* **Verified** sections were run against oxi-gen v0.5.1 built from source, Lutra v0.6.21 and
  this repository's tests (`tests/test_oxigen.py`).
* **Proposal** sections describe changes that oxi-gen does not have yet.

| Level | What oxi-gen does | Change to oxi-gen | Status |
|---|---|---|---|
| [0](#level-0-compile-templates-to-tarql-no-change-to-oxi-gen) | runs a query compiled from a template | none | **verified** |
| [1](#level-1-lists-and---split) | treats `--split` as OTTR's `cross` | bind the index of each split value (a few lines) | proposal |
| [2](#level-2-oxi-gen-emits-ottr-instances) | prints OTTR instances for Lutra or maplib to expand | `SELECT` support plus an instance writer | proposal |
| [3](#level-3-native---library----template) | reads stOTTR itself and compiles it to its own CONSTRUCT | stOTTR parser and template compiler | proposal |
| [4](#level-4-a-bottr-mapping-file-instead-of-flags) | reads a bOTTR-style mapping file | mapping reader on top of level 3 | proposal |
| [5](#level-5-expand-without-per-row-sparql) | expands templates per row without evaluating a query | a compiled emission plan | proposal |

The running example is in [`examples/oxigen/`](../examples/oxigen).

## The example

`people.csv`, where one cell (`skills`) holds a list:

```csv
person,name,email,manager,skills
ex:alice,Alice,alice@example.com,ex:carol,python;rust;sparql
ex:bob,Bob,,ex:carol,sparql
ex:carol,Carol,carol@example.com,,
```

`people.stottr`, an OTTR template library:

```stottr
ex:Person [ ottr:IRI ?person, xsd:string ?name, ? xsd:string ?email, ? ottr:IRI ?manager ] :: {
    ottr:Triple(?person, rdf:type, schema:Person),
    ottr:Triple(?person, schema:name, ?name),
    ottr:Triple(?person, schema:email, ?email),
    ex:ReportsTo(?person, ?manager)
} .

# ?manager is mandatory: without a manager none of these triples may appear,
# including the one about the blank node.
ex:ReportsTo [ ottr:IRI ?person, ottr:IRI ?manager ] :: {
    ottr:Triple(?person, ex:reportsTo, ?manager),
    ottr:Triple(?person, ex:reporting, _:r),
    ottr:Triple(_:r, ex:manager, ?manager),
    ottr:Triple(_:r, ex:source, "hr.csv")
} .

ex:HasSkill [ ottr:IRI ?person, xsd:string ?skill ] :: { ottr:Triple(?person, ex:skill, ?skill) } .

# One ex:HasSkill per skill, but one record node per person.
ex:Employee [ ottr:IRI ?person, xsd:string ?name, ? xsd:string ?email, ? ottr:IRI ?manager,
              ? List<xsd:string> ?skills ] :: {
    ex:Person(?person, ?name, ?email, ?manager),
    cross | ex:HasSkill(?person, ++?skills),
    ottr:Triple(?person, ex:record, _:rec),
    ottr:Triple(_:rec, ex:source, "hr.csv")
} .
```

Why this is worth having in oxi-gen: today each mapping is a single query, and the same
patterns get repeated across files. Templates give those patterns names, typed parameters and a
library that Lutra can lint and document. Running `ottr-tarql decompose` over a set of existing
queries finds the shared patterns automatically (see [DESIGN.md](DESIGN.md)).

---

## Level 0: compile templates to TARQL (no change to oxi-gen)

**Verified.** `ottr-tarql compose` flattens a template into the kind of query oxi-gen already
runs. Each parameter is read from the CSV column of the same name, and IRI-typed parameters
accept either a prefixed name or a full IRI.

```sh
ottr-tarql compose examples/oxigen/people.stottr -T ex:Person -o person.rq
oxi_gen -q person.rq -i examples/oxigen/people.csv
```

The generated [`person.rq`](../examples/oxigen/person.rq) (abridged):

```sparql
CONSTRUCT {
  ?person_iri ?_g1 schema:Person ; schema:name ?name ; ?_g2 ?email ;
              ?_g4 ?manager_iri ; ?_g5 _:b3 .
  _:b3 ?_g6 ?manager_iri ; ?_g7 "hr.csv" .
}
WHERE {
  BIND(IF(CONTAINS(STR(?person), "://"), IRI(?person), tarql:expandPrefixedName(?person)) AS ?person_iri)
  BIND(IF(CONTAINS(STR(?manager), "://"), IRI(?manager), tarql:expandPrefixedName(?manager)) AS ?manager_iri)
  BIND(IF(sameTerm(?name, ?name), rdf:type, ?_unbound) AS ?_g1)
  ...
  BIND(IF(sameTerm(?manager_iri, ?manager_iri) && sameTerm(?name, ?name)
          && sameTerm(?person_iri, ?person_iri), ex:source, ?_unbound) AS ?_g7)
}
```

The `?_gN` predicates are **guards**. In OTTR, an instance with a missing mandatory argument is
dropped entirely, including its triples about blank nodes and constants. SPARQL would otherwise
still emit those triples. Carol has no manager, so she gets no `ex:reporting` node.

oxi-gen's output is the same graph that Lutra produces from the equivalent OTTR instances: 16
triples, compared up to blank-node renaming. `tests/test_oxigen.py` checks this, plus a
decompose → compose round trip of every oxi-gen test fixture.

### What a template compiler has to respect in oxi-gen

These came out of running against the real binary. Anything that generates queries for
oxi-gen (this tool, or a level-3 implementation) needs to know them:

| Behaviour | Consequence |
|---|---|
| CSV values are **substituted into the query** (spareval's `substitute_variable`), not bound as solutions | `BOUND(?column)` is **always false**, even when the cell has a value. `COALESCE(?column, …)` and `BOUND` on a BIND result work normally. Generated guards therefore use `sameTerm(?v, ?v)`. Probe: [`tests/fixtures/extra/bound.rq`](../tests/fixtures/extra/bound.rq). It may be worth raising upstream. |
| The `tarql:` prefix is predeclared, and prefixes are read by a regex over the query text | `tarql:expandPrefixedName` needs `PREFIX p: <ns>` written with whitespace before `<`. |
| Only variables that appear in the query text (outside `#` comments) are bound | Columns that the template doesn't use are ignored. |
| `?ROWNUM` is 0-based `xsd:integer` | |
| `\` escapes the next character inside quoted CSV fields, and empty or whitespace-only cells are unbound | Use `--bind-empty-strings` to keep empty strings. |

### Limits of level 0

* **Lists.** `ex:Employee` cannot be compiled:
  `parameter ?skills has list type; a TARQL row has one value per column`. See level 1.
* **No run-time type checks.** `STRDT("abc", xsd:decimal)` happily produces an ill-typed literal.
  Lutra would refuse it. See [Validation](#validation-with-shacl).
* **Two tools.** The compiled `.rq` file is a build artefact that has to be kept in sync with the
  templates.

---

## Level 1: lists and `--split`

oxi-gen already has a list feature. `--split skills skill ";"` re-runs the query once for each
value. With several `--split`s you get every combination of their values, which is OTTR's
`cross`. The difference is scope: oxi-gen repeats **the whole query**, while `cross` repeats only
the one instance it is attached to. Blank nodes show the difference (**verified**):

```sparql
CONSTRUCT { ?person ex:skill ?skill ; ex:record [ ex:source "hr.csv" ] . } WHERE { … }
```

```text
$ oxi_gen -q split.rq -i split.csv --split skills skill ";"      # one row, three skills
# (N-Triples output, condensed)
ex:alice ex:skill "python", "rust", "sparql" .
ex:alice ex:record _:d019… , _:eec1… , _:a45a… .                   # three record nodes, not one
```

The same data through standard **bOTTR** and Lutra, using
[`employees.bottr.ttl`](../examples/oxigen/employees.bottr.ttl), gives the OTTR semantics: three
skills and **one** record node per person. Bob, with one skill, and Carol, with none, come out
correctly as well (**verified**):

```sh
java -jar lutra.jar -m expand -I bottr -l examples/oxigen -L stottr -e stottr examples/oxigen/employees.bottr.ttl
```

**Proposal: bind the index of each split value.** In `apply_split`, as well as pushing
`(split, value)`, also push `(format!("{split}_INDEX"), i.to_string())`. That is about five
lines. With it, a compiler can translate `cross | ex:HasSkill(?person, ++?skills)` exactly:

* emit `--split skills skill ";"`;
* leave the triples inside the expanded instance unguarded;
* guard every other triple with `?skill_INDEX = "0"`, so it is produced once per row, not once per
  value.

This works under substitution because it is a comparison, not `BOUND`. `zipMin` over two lists
becomes the guard `?a_INDEX = ?b_INDEX`. `zipMax` would also need the length of each list
(`{split}_COUNT`).

---

## Level 2: oxi-gen emits OTTR instances

**Proposal.** In this level oxi-gen does only the *lifting*, meaning CSV values to typed
template arguments, and Lutra or [maplib](https://github.com/DataTreehouse/maplib) does the
expansion:

```sh
oxi_gen -q person-args.rq --ottr-template ex:Person -i people.csv -o people.stottr
lutra -m expand -l templates/ -I stottr people.stottr
```

`person-args.rq` is a `SELECT` whose projection is in parameter order. Each solution becomes one
instance, and an unbound variable becomes `none`:

```stottr
ex:Person(ex:alice, "Alice", "alice@example.com", ex:carol) .
ex:Person(ex:bob, "Bob", none, ex:carol) .
ex:Person(ex:carol, "Carol", "carol@example.com", none) .
```

What it needs:

* accept `SELECT` queries alongside `CONSTRUCT`;
* a stOTTR instance writer (escaping, prefixes, `none`, and list terms such as `("python", "rust")`).
* A `--split-list COL SEP` option that binds a column as one **list** value rather than
  multiplying rows. That gives level 1's semantics with no guards at all.

This already works today without oxi-gen: `ottr-tarql instances` produces these instances from
the CSV.

---

## Level 3: native `--library` / `--template`

**Proposal.** oxi-gen reads stOTTR and compiles the template once, at start-up, into the
`CONSTRUCT` query its hot loop already evaluates. So this is level 0 moved inside oxi-gen, with
lists handled natively:

```sh
oxi_gen --library templates/ --template ex:Employee --list skills ";" -i people.csv
```

What it needs, as a Rust crate (`ottr`) that oxi-gen depends on:

| Component | Notes |
|---|---|
| stOTTR parser and library loader | Directories of `.stottr` files and prefixed template names. maplib's `templates` crate parses stOTTR, but it is Apache-2.0 only (see the licence note below). |
| type checker | Lutra-compatible rules: non-blank (`!`) consistency, `ottr:IRI` in subject and predicate positions, the list types. Run `lutra -m lint` over the same library as a differential test. |
| template compiler | the semantics in [DESIGN.md §3](DESIGN.md): `none`, optional parameters, defaults (`COALESCE`), mandatory-parameter guards (`sameTerm`), blank nodes fresh per instance, constant lists to RDF collections, list expanders |
| argument conversion | prefixed name or IRI to `ottr:IRI`, `STRDT` for XSD types, language tags, plus null and list handling from CSV strings |
| CLI | `--library`, `--template`, `--list COL SEP`. Existing flags (delimiter, `--test`, `--dedup`, gzip) keep working, because the hot loop is unchanged. |
| tests | Port the fixtures in this repository. The oracles are this tool's `compose` and Lutra's expansion. |

Shape of the API (a sketch, not compiled):

```rust
pub struct Library { /* templates by IRI, prefixes */ }
pub struct CompileOptions { pub list_columns: HashMap<String, String>, /* column -> separator */ }

impl Library {
    pub fn load(paths: &[PathBuf]) -> Result<Self, OttrError>;
    /// The query oxi-gen already knows how to run, plus the columns it reads.
    pub fn compile_to_construct(&self, root: &NamedNode, opts: &CompileOptions)
        -> Result<(spargebra::Query, Vec<String>), OttrError>;
}
```

**Licence note.** oxi-gen is Apache-2.0. HOLOS (`pwin/triplestore`) is MIT OR Apache-2.0, and
it records that Apache-only code cannot be copied into it. A shared `ottr` crate should be
**MIT OR Apache-2.0** so both projects can use it. This repository is licensed that way, apart from the
oxi-gen test fixtures it copies.

---

## Level 4: a bOTTR mapping file instead of flags

**Proposal.** bOTTR is OTTR's own standard for "query a source, then instantiate a template".
Its argument maps already cover what oxi-gen does with flags: `ottr:type`, `ottr:nullValue`,
`ottr:languageTag`, `ottr:listSep`. oxi-gen could accept a bOTTR file whose source is the CSV
and whose query is TARQL-style SPARQL instead of SQL:

```turtle
[] a ottr:InstanceMap ;
  ottr:template ex:Employee ;
  ottr:source [ a oxigen:CSVSource ; ottr:sourceURL "people.csv" ] ;   # extension
  ottr:query """
    SELECT ?p ?name ?email ?m ?skills WHERE {
      BIND(tarql:expandPrefixedName(?person) AS ?p)
      BIND(tarql:expandPrefixedName(?manager) AS ?m)
    }""" ;
  ottr:argumentMaps ( [ ottr:type ottr:IRI ] [ ] [ ] [ ottr:type ottr:IRI ]
                      [ ottr:type ( rdf:List xsd:string ) ; ottr:listSep ";" ] ) .
```

The standard version of this file, with H2 SQL as the source, runs in Lutra today:
[`employees.bottr.ttl`](../examples/oxigen/employees.bottr.ttl). It uses one workaround that
oxi-gen could drop: bOTTR requires one-character `ottr:listStart`/`ottr:listEnd`, so the SQL wraps
the cell as `'(' || skills || ')'`. oxi-gen could accept unbracketed lists as an extension.

What it needs, on top of level 3:

* a Turtle reader for the mapping (oxi-gen already depends on `oxrdfio`);
* the argument-map semantics from bOTTR §2.4;
* one new source type.

The mapping then documents the whole job in one file: template, source, lifting, types and
lists.

---

## Level 5: expand without per-row SPARQL

**Proposal; performance.** Levels 0 and 3 still evaluate a CONSTRUCT query for every row, guards
included. Instead, the compiler could produce a **plan**:

* a flat list of triple patterns over argument slots;
* a guard set for each pattern (the slots that must be bound);
* a blank-node count for each instance.

For each row, oxi-gen would then:

* compute the argument values, using spareval only for real expressions and native code for
  plain casts;
* split any list columns;
* emit the triples directly.

```rust
pub struct Plan { pub patterns: Vec<[Slot; 3]>, pub guards: Vec<Vec<SlotId>>, pub bnodes: usize }
impl Plan { pub fn emit(&self, args: &[Option<Term>], out: &mut Vec<Triple>) { /* … */ } }
```

maplib takes this approach in columnar form, expanding templates over Polars data frames. It is
both prior art and a possible alternative back end. No benchmark exists yet for oxi-gen. The
first step would be to compare the level-0 query against this plan on the oxi-gen test data,
using a `--release` build.

---

## Validation with SHACL

Templates already say what the output should look like:

* the classes of their subjects;
* the predicates they use;
* `ottr:IRI` versus datatypes;
* which parameters are mandatory.

So `ottr-tarql shapes` generates **SHACL shapes** from them, and those shapes check oxi-gen's
output. That catches what the SPARQL route lets through (see [DESIGN.md §5](DESIGN.md)):

```sh
ottr-tarql shapes examples/oxigen/people.stottr -T ex:Employee -o shapes.ttl
```

In a trial, a row with `price = "abc"` and `launched = "01/02/2024"` produced
`"abc"^^xsd:decimal` and `"01/02/2024"^^xsd:date`. [SHACL_Engine](https://github.com/pwin/SHACL_Engine)
(`pip install shacl`) reported both violations in about a millisecond. In a
[HOLOS](https://github.com/pwin/triplestore) deployment the same shapes can be the boundary of a
holon: map into a staging graph, then tick the delta in, and the boundary admits or refuses it.

---

## Suggested order

1. **Now:** level 0. Keep templates in a library, compile with `ottr-tarql compose`, run with
   oxi-gen, and test with `OXI_GEN=… pytest`.
2. **Small oxi-gen PRs:** the split index (level 1) and `SELECT` with instance output (level 2).
   Each is small, and together they make lists exact.
3. **The `ottr` crate (MIT OR Apache-2.0)** with the parser and compiler, used by oxi-gen for
   level 3 and available to HOLOS.
4. **Then** the bOTTR mapping (level 4), and a benchmark to decide whether level 5 pays for
   itself.

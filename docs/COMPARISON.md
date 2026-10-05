# Native and converted mappings compared

This is an experiment with three CSV files that are hard to map. The same CSV → RDF
transformation is written twice by hand, once as TARQL queries for oxi-gen and once as a
modular OTTR library. Then each is converted into the other form with `ottr-tarql`. The
questions are:

1. Do all the routes produce the same RDF?
2. How do the converted mappings compare with the hand-written ("native") ones?
3. What does strong typing buy when the data is bad?

Everything here is checked by [`tests/test_retail.py`](../tests/test_retail.py). The numbers
come from [`converted/METRICS.md`](../tests/fixtures/retail/converted/METRICS.md), which the
tests regenerate. The results were verified with oxi-gen v0.5.1, Lutra v0.6.21 and
SHACL_Engine 0.3.2.

```sh
OXI_GEN=path/to/oxi_gen pytest tests/test_retail.py                 # run the comparison
UPDATE_RETAIL=1 OXI_GEN=path/to/oxi_gen pytest tests/test_retail.py # regenerate converted/ and expected/
```

Without `OXI_GEN`, the oxi-gen tests are skipped and the rdflib emulator stands in for it.
Without Java and `tools/lutra.jar`, the built-in expander stands in for Lutra.

## The files

All under [`tests/fixtures/retail/`](../tests/fixtures/retail):

| Path | What it is |
|---|---|
| `customers.csv`, `products.csv`, `orders.csv` | the data |
| `tarql/*.rq` | **native TARQL**: one hand-written query per CSV |
| `ottr/base.stottr`, `ottr/domain.stottr`, `ottr/rows.stottr` | **native OTTR**: a hand-written library |
| `expected/*.nt` | the reference output: oxi-gen running the native TARQL |
| `converted/tarql-from-ottr/` | `compose` of the native OTTR |
| `converted/ottr-from-tarql/` | `decompose` of the native TARQL |
| `converted/ottr-round-trip/` | `decompose` of `converted/tarql-from-ottr/` (OTTR → TARQL → OTTR) |
| `converted/METRICS.md` | the measurements quoted below |

## The data

| CSV | Rows | Columns | What one row makes |
|---|---:|---:|---|
| customers | 6 | 18 | a customer, an optional postal address node, an optional account node, and a parent link in both directions |
| products | 7 | 14 | a product, an optional price node with a default currency, an optional weight node, an optional "discontinued" type, and a successor link in both directions |
| orders | 8 | 15 | an order (repeated over its lines and merged), a line node, and an optional unit price node and an optional shipment node under the line |

The cells cover:

* quoted commas, line breaks and tabs, `""` and `\"` quotes, and a `\&` escape;
* Unicode and emoji;
* padding that must be kept (`  Initech  `), and cells that hold only spaces or a tab, which
  count as empty;
* ids written as prefixed names or as full IRIs;
* leading zeros that must stay (`0155`);
* numbers not in canonical form (`129.90`, `250000.00`, `5.0`), dates, `xsd:gYear`, and
  date-times with `Z`, offsets and fractions of a second;
* booleans written as `true`, `false`, `1` and `0`;
* an order with no lines.

## The two native mappings

**Native TARQL** is written the way an oxi-gen user would write it:

* `COALESCE(tarql:expandPrefixedName(?id), IRI(?id))` for ids that may be either form.
* Casts (`xsd:decimal(?price)`) for numbers, booleans and date-times. `STRDT` for dates and
  years, because oxi-gen has no `xsd:date()` or `xsd:gYear()` cast: SPARQL 1.1 does not
  require one, and oxi-gen panics on it.
* `IF(BOUND(?amount), BNODE(), ?none)` for each optional node, applied to the *BIND result*.
  oxi-gen substitutes cell values into the query, so `BOUND(?column)` is always false. For a
  raw column the query uses `COALESCE(?locality, "") != ""` instead.
* Nested optional nodes repeat the condition of their parent: `BOUND(?item) && BOUND(?ship_date)`.

**Native OTTR** has three layers:

* `base.stottr`: generic templates (`rt:Value`, `rt:Typed`, `rt:Inverse`) whose values are
  untyped. This is the "regular" typing.
* `domain.stottr`: strongly typed templates (`rt:Price [ … xsd:decimal ?amount, ! ottr:IRI ?currency = cur:EUR ]`,
  `rt:Shipment [ … xsd:date ?shippedOn … ]`). A node exists only if its defining value
  does, because that value is a mandatory parameter.
* `rows.stottr`: one root per CSV whose parameters are the CSV columns. The type of a
  parameter decides how `compose` reads the cell.

Lutra's type checker fixes the direction of the mix. A typed value can go into an untyped
parameter, but an untyped value going into a typed parameter is an error
(`incompatible argument and parameter type`). So types are declared at the root and the
domain layer, and only the generic leaves are untyped. The non-blank flag behaves the same
way: `rt:Price`'s currency is `!`, so every parameter that feeds it must be `!` as well.

## 1. Same output on every route

Every route produces the reference graph (105, 108 and 111 triples), **term for term**:

| Route | Runs on |
|---|---|
| native TARQL | oxi-gen (the reference), rdflib emulator |
| native OTTR → instances | Lutra, built-in expander |
| TARQL from OTTR (`compose`) | oxi-gen, emulator |
| OTTR from TARQL (`decompose`) → instances | Lutra, built-in expander |
| TARQL → OTTR → TARQL | oxi-gen |
| OTTR → TARQL → OTTR → instances | Lutra |

"Term for term" includes lexical forms. The reference file holds oxi-gen's output as written,
and the tests read it without rdflib's literal normalisation.

That needed one fix. oxi-gen writes each typed value an expression produces in canonical
form, from casts and from `STRDT` alike: `"129.90"` becomes `"129.9"^^xsd:decimal`, `"1"`
becomes `true`, and `"…+00:00"` becomes `"…Z"`. Lutra keeps whatever form it is given. So
`ottr-tarql instances`, and the emulator, now write values the way oxi-gen does
(`ottr_tarql/literals.py`, checked against oxi-gen cell by cell in `tests/test_oxigen.py`).
Before that, the OTTR routes differed from oxi-gen in every non-canonical decimal and every UTC
date-time. Those are the same values, but different RDF terms, so `sameTerm`, a join with data
loaded elsewhere, or a graph diff would all have seen them as different.

## 2. How the converted mappings compare

### TARQL from OTTR vs native TARQL

| | customers | products | orders |
|---|---|---|---|
| CONSTRUCT triple patterns (native / composed) | 25 / 25 | 22 / 22 | 22 / 22 |
| BINDs | 12 / 22 | 15 / 22 | 14 / 28 |
| of which guards | 0 / 12 | 0 / 9 | 0 / 16 |
| lines | 50 / 68 | 50 / 65 | 53 / 73 |

The patterns are the same, but the conditions are written differently:

* **Native TARQL** decides once per node whether the node exists: one `IF(BOUND(…), BNODE(), ?none)`
  per optional node.
* **Composed TARQL** decides per triple. Each triple that does not mention every mandatory
  value it depends on gets a guard predicate:
  `?_g13 = IF(sameTerm(?line_typed, ?line_typed) && sameTerm(?order_iri, ?order_iri) && …, ex:shipment, ?_unbound)`.
  This is exact, and it also covers the root's mandatory id, which the native query does
  not check. But half of the composed query is guards.

Other differences:

* The composed query reads every IRI column with `IF(CONTAINS(STR(?c), "://"), IRI(?c), tarql:expandPrefixedName(?c))`,
  so prefixed names and full IRIs both work in every column. The native query accepts both
  forms only where its author expected them.
* Every typed column becomes `STRDT`, never a cast. That makes no difference on good data.
  On bad data it changes what happens (§3).
* The stOTTR library has to declare the prefixes used *in the data* (`cust:`, `prod:`, …).
  They become the composed query's `PREFIX`es, which `tarql:expandPrefixedName` needs.

### OTTR from TARQL vs native OTTR

| | native | from TARQL |
|---|---:|---:|
| templates (not counting roots) | 13 | 11 |
| used by more than one CSV | 4 | 1 |
| nesting depth, root to `ottr:Triple` | 4 | 2 |
| mandatory parameters | 30 | 11 |
| non-blank parameters | 10 | 0 |

* **Flat, one template per subject node.** `decompose` cuts the query by subject, so each
  template is a list of `ottr:Triple`s. Only `tpl:PriceSpecification` is shared, between
  products and orders. Factoring found no template contained in another.
* **Nodes are passed in, not made.** The native `rt:Address` creates its blank node and
  makes it conditional on a mandatory `?locality`. `tpl:PostalAddress` takes the node as its
  subject parameter. The decision "this row has an address" stays in the WHERE clause, kept
  in a `tq:Bind` annotation.
* **No generalisation of constants.** The native library has one `rt:Inverse` for both
  `schema:parentOrganization`/`subOrganization` and `ex:replaces`/`replacedBy`. `decompose`
  makes two templates, `tpl:ParentOrg` and `tpl:Predecessor`.
* **Types.** Across the 48 columns:

  | Outcome | Columns | Why |
  |---|---:|---|
  | same type | 33 | casts and `STRDT` give the datatype; `tarql:expandPrefixedName` gives `ottr:IRI` |
  | type lost | 4 | `currency` (products and orders), `customer`, `product`: values built with `COALESCE(…)`. `decompose` types a BIND by its outermost function only. |
  | generalised | 3 | `kind`, `category` (`owl:Class`) and `status` (`owl:NamedIndividual`) become `ottr:IRI`. A query cannot say a value is a class. |
  | type added | 8 | columns used as they are become `xsd:string`, and `?ROWNUM` becomes `xsd:integer`, where the native templates left them untyped |

  Ids built with `COALESCE` keep `ottr:IRI` only because they are subjects.

### Round trips

* **TARQL → OTTR → TARQL is lossless.** It gives back the same prefixes, the same
  triple patterns and the same BINDs in the same order, and oxi-gen output that is exactly
  the same. Only comments and layout are lost.
* **OTTR → TARQL → OTTR keeps the meaning but not the design.** The output is still the
  same graph. But the guards turn constant predicates into variables, so `decompose` sees
  predicate parameters. The library grows from 68 to 97 parameters, 34 of them
  `!? ottr:IRI ?predicateN`. Templates get names such as `tpl:Node_4`, and
  `tpl:Node_4`'s properties are arguments instead of `schema:addressLocality` and so on.
  Modularity cannot be recovered from the flat query.

## 3. Strong and regular typing on bad data

`test_bad_cells` breaks one or two cells per CSV and runs every route:

| Bad cell | Native TARQL on oxi-gen | TARQL from OTTR on oxi-gen | Native OTTR on Lutra |
|---|---|---|---|
| `credit_limit = 5k`, `stock_qty = lots`, `qty = one` (cast in the native query) | the value is dropped without a message | kept as `"5k"^^xsd:decimal` | expansion refused: `The value '5k' is not in the lexical space of its datatype` |
| `price = N/A` (cast) | the **whole price node** is dropped, because it depends on the cast | a price node with `"N/A"^^xsd:decimal` | refused |
| `born = 28/02/1985` (`STRDT` in the native query) | kept as `"28/02/1985"^^xsd:date` | kept | refused |

The SHACL shapes generated from the native OTTR (`ottr-tarql shapes`) flag every ill-typed
literal in both TARQL outputs. They cannot see a value that a cast dropped, because no
triple is left to check. In this native TARQL, a cast is silent data loss and `STRDT` is
detectable bad data. Strong types make the OTTR route fail loudly in Lutra. With the shapes,
they also make the TARQL routes checkable. A regular (untyped) column is checked nowhere: any
text goes through as a string.

## 4. Issues found

| Where | Issue | Status |
|---|---|---|
| emulator (`run`), `instances` | Lexical forms followed rdflib, not oxi-gen. Decimals kept the cell's form, where oxi-gen writes `129.90` as `129.9`. Date-times were rewritten (`Z` as `+00:00`), which oxi-gen does not do. | Fixed (#4): values take oxi-gen's forms, and every route now matches term for term. |
| emulator | rdflib's `xsd:dateTime("2019-03-01")` gave `2019-03-01T00:00:00` where oxi-gen gives an error, so the graphs differed. | Fixed (#4): strict casts during evaluation. |
| `decompose` | A BIND is typed by its outermost function, so `COALESCE(tarql:expandPrefixedName(…), IRI(…))` and `IF(…)` get no type. | Open |
| `decompose` | After `compose`, guards come back as predicate parameters. | Open |
| `decompose` | `tpl:Product`'s subject parameter is named `?catalogItem`, because two different item orders are used for naming. | Open |
| CLI | Writing non-ASCII to a Windows console fails (`'charmap' codec can't encode character`), for example `ottr-tarql run` on these CSVs without `-o`. | Open |
| stOTTR parser | Accepts multi-line `/* … */` comments, which Lutra rejects. The library uses `#` comments. | Open |
| oxi-gen | `xsd:date(…)` panics (`UnsupportedCustomFunction`) instead of reporting an error. | Upstream |

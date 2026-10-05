# ottr-tarql

Converts in both directions between **TARQL / oxi-gen SPARQL CONSTRUCT** mappings and **OTTR templates** (stOTTR),
and generates **SHACL shapes** from templates to check the RDF they produce. It works with **RDF 1.1 and
SPARQL 1.1**. New here? Read the short [manual](docs/MANUAL.md).

* `decompose`: takes a *set* of `.rq` files and produces a shared library of reusable OTTR templates, plus one
  root template per query. Shapes that are identical across files are merged, and larger shapes are rewritten to
  call smaller ones.
* `compose`: takes an OTTR template (round-tripped or hand-written) and produces a TARQL query that oxi-gen or TARQL can run.
* `shapes`: takes OTTR templates and produces SHACL shapes describing their output. Use it to validate oxi-gen output,
  for example with [SHACL_Engine](https://github.com/pwin/SHACL_Engine) (`pip install shacl`).

The design is described in [docs/DESIGN.md](docs/DESIGN.md): how the semantics line up, the algorithms, and the limitations.
How oxi-gen can use OTTR templates, from what works today to native support, is in
[docs/OXIGEN-INTEGRATION.md](docs/OXIGEN-INTEGRATION.md), with a runnable example in [examples/oxigen](examples/oxigen).

## Install

```sh
pip install -e .[test]          # Python ≥ 3.10, depends on rdflib
# optional, for the Lutra tests and for expanding instances with the reference implementation:
curl -L --create-dirs -o tools/lutra.jar https://www.ottr.xyz/downloads/lutra/lutra-v0.6.21.jar
```

## Usage

```sh
# TARQL -> OTTR
ottr-tarql decompose mappings/*.rq -o out/ --template-ns http://example.com/tpl/ --query-ns http://example.com/q/
#   out/templates.stottr        shared templates
#   out/queries/<name>.stottr   one root template per query (its WHERE clause is kept in @@tq: annotations)
#   out/tq-vocabulary.stottr    signatures of the tq: annotations

# OTTR -> TARQL
ottr-tarql compose out/ --all -o regenerated/              # every root made by decompose
ottr-tarql compose my-templates/ -T ex:Product -o product.rq   # any template; params are read from CSV columns of the same name

# OTTR -> SHACL, to check what comes out
ottr-tarql shapes my-templates/ -T ex:Product -o shapes.ttl

# Run things
ottr-tarql run -q product.rq -i products.csv                   # rdflib emulation of oxi-gen
ottr-tarql instances out/ -T q:people -i people.csv -o people.stottr
java -jar tools/lutra.jar -m expand -l out -L stottr -e stottr -I stottr -O wottr people.stottr
ottr-tarql expand out/ --instances people.stottr               # built-in expander, no Java needed
```

CSV options for `run` and `instances` follow oxi-gen: `-d` delimiter, `-t` TSV, `-H` no header row, `-p` escape character, `--quote-char`.

## Tests

```sh
pytest
```

The tests check, up to graph isomorphism, that the original query, the round-tripped query, and Lutra's expansion
of the decomposed templates all produce the same RDF from the same CSV. They also check that a hand-written OTTR
library composes to SPARQL that agrees with Lutra. If `java` or `tools/lutra.jar` is missing, the Lutra tests are
skipped.

Set `OXI_GEN` to an oxi-gen binary (or put `oxi_gen` on the `PATH`) to also run the queries through the real oxi-gen:
`run` (the rdflib emulation of oxi-gen) and every composed query are then checked against it.

`tests/test_retail.py` is a larger experiment on three complex CSV files. The same transformation is written by
hand twice, as TARQL for oxi-gen and as a modular OTTR library that mixes untyped and strongly typed templates.
Each is converted into the other form, and every route is checked against oxi-gen's output. The results,
including how the converted mappings differ from the hand-written ones, are in
[docs/COMPARISON.md](docs/COMPARISON.md).

The fixtures in `tests/fixtures/oxigen` come from [semanticarts/oxi-gen](https://github.com/semanticarts/oxi-gen)
(Apache-2.0; see `tests/fixtures/oxigen/LICENSE`).

## Licence

Dual licensed under [MIT](LICENSE-MIT) or [Apache 2.0](LICENSE-APACHE), at your option.
Copyright (c) 2026 pwin (Peter Winstanley).

The exception is `tests/fixtures/oxigen`. Those files are copied from oxi-gen and remain under its Apache-2.0
licence only (see `tests/fixtures/oxigen/LICENSE`).

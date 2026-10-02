# ottr-tarql: a short manual

`ottr-tarql` moves CSV-to-RDF mappings between two forms:

* **TARQL queries** (`.rq`): SPARQL `CONSTRUCT` files that [oxi-gen](https://github.com/semanticarts/oxi-gen)
  runs once for each row of a CSV.
* **OTTR templates** (`.stottr`): small, named, reusable patterns ([ottr.xyz](https://ottr.xyz)).

It can also make **SHACL shapes** from templates, to check the RDF you produce. It works with
RDF 1.1 and SPARQL 1.1.

## Install

```sh
pip install -e .      # in a clone of this repository
pip install shacl     # optional: for checking output against shapes
```

Every command explains itself: `ottr-tarql <command> --help`.

## The example

The files are in [`examples/oxigen`](../examples/oxigen). `people.csv` (shortened):

```csv
person,name,email,manager,skills
ex:alice,Alice,alice@example.com,ex:carol,python;rust;sparql
ex:carol,Carol,carol@example.com,,
```

`people.stottr` (part of it). `ex:Person` takes four values. A `?` in front marks a value as
optional:

```stottr
ex:Person [ ottr:IRI ?person, xsd:string ?name, ? xsd:string ?email, ? ottr:IRI ?manager ] :: {
    ottr:Triple(?person, rdf:type, schema:Person),
    ottr:Triple(?person, schema:name, ?name),
    ottr:Triple(?person, schema:email, ?email),
    ex:ReportsTo(?person, ?manager)
} .
```

## 1. Template → query

```sh
ottr-tarql compose people.stottr -T ex:Person -o person.rq
oxi_gen -q person.rq -i people.csv -o people.ttl
```

Things to know:

* **Columns.** The CSV columns must have the same names as the template's values (`person`,
  `name`, …).
* **IRIs.** An `ottr:IRI` value can be written as `ex:alice` or as a full `http://…` IRI.
* **Typed values.** Values such as `xsd:date` get that datatype.
* **Empty cells.** An empty cell means "no value". If a value is required (no `?`), the template
  produces nothing for that row.

## 2. Queries → templates

```sh
ottr-tarql decompose mappings/*.rq -o templates/
```

```text
templates/
  templates.stottr       patterns shared by the queries, one template each
  queries/people.stottr  one "root" template per query
  tq-vocabulary.stottr   definitions the root templates refer to
```

Patterns that repeat across your queries become one shared template. Each root template keeps the
query's `WHERE` clause in `@@tq:` lines, so you can always get the queries back:

```sh
ottr-tarql compose templates/ --all -o queries/
```

## 3. Check the output

Make SHACL shapes from the templates, then validate the RDF against them:

```sh
ottr-tarql shapes people.stottr -T ex:Person -o shapes.ttl
```

```python
import shacl

report = shacl.Shapes.from_file("shapes.ttl").validate_file("people.ttl")
print(report.conforms)
for r in report.results:
    print(r.focus_node, r.path, r.value, r.component)
```

The shapes catch, for example:

* a missing required value;
* a literal where an IRI belongs;
* a malformed value such as `"abc"^^xsd:decimal`.

Options:

* `-T` names the templates you really use. Without it, every template counts, and the shapes are
  looser.
* `--max-counts` also limits how many values a property may have.
* `--allow-subtypes` accepts, for example, `xsd:int` where `xsd:integer` is declared.

## 4. Without oxi-gen

| Command | What it does |
|---|---|
| `ottr-tarql run -q person.rq -i people.csv -o people.ttl` | Runs a query over a CSV in Python, the way oxi-gen would |
| `ottr-tarql instances people.stottr -T ex:Person -i people.csv -o people.inst.stottr` | Turns CSV rows into OTTR instances |
| `ottr-tarql expand people.stottr --instances people.inst.stottr -o people.ttl` | Turns OTTR instances into RDF |

For OTTR's reference tool, use Lutra instead of `expand`. Here `templates/` is the folder that
holds your `.stottr` templates:

```sh
java -jar lutra.jar -m expand -l templates/ -L stottr -e stottr -I stottr -o people.ttl people.inst.stottr
```

## 5. From Python

```python
from ottr_tarql import Library, parse_stottr, parse_query, compose, decompose, generate_shapes, serialize_query

# queries -> templates
decompose([parse_query(open("people.rq").read(), name="people")]).write("templates/")

# template -> query
lib = Library([parse_stottr(open("people.stottr").read())])
print(serialize_query(compose(lib, "ex:Person").query))

# template -> SHACL shapes (an rdflib Graph)
generate_shapes(lib, ["ex:Person"]).serialize("shapes.ttl", format="turtle")
```

## Limits

* **Lists.** A list value, such as `skills` holding `python;rust`, can't become a query yet. Use
  Lutra with the bOTTR mapping in `examples/oxigen/employees.bottr.ttl`.
* **RDF 1.1 only.** The tool works with RDF 1.1 and SPARQL 1.1. Queries that use RDF 1.2 syntax
  (`~`, `{| |}`, `<< >>`) are rejected with a message.
* **More detail.** See [README](../README.md), [DESIGN.md](DESIGN.md) and
  [OXIGEN-INTEGRATION.md](OXIGEN-INTEGRATION.md).

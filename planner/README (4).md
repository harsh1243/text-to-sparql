# SPARQL Query Planner

Converts a SPARQL query into a structured, step-by-step execution plan. Built on top of [rdflib](https://rdflib.readthedocs.io/) and designed to work with both [LC-QuAD 1.0](http://lc-quad.sda.tech/) (DBpedia) and [LC-QuAD 2.0](http://lc-quad.sda.tech/) (Wikidata), though it works on any valid SPARQL query.

The plan output is a JSON list of semantic steps — useful as training data for question-answering models or as a readable breakdown of what a query is actually doing.

---

## Files

### `sparql_planner.py` — Core Planner

This is the main file. Given a SPARQL query string, it produces a structured plan.

**Entry point:**

```python
from sparql_planner import build_plan

plan = build_plan(sparql_query, question="Who directed Inception?")
print(plan)
```

**What it does internally:**

1. **Parse the query** — Uses `rdflib.plugins.sparql.parser.parseQuery()` to turn the raw SPARQL string into a parse tree.

2. **Build the algebra tree** — Passes the parse tree through `rdflib.plugins.sparql.algebra.translateQuery()`, which produces a relational algebra representation (`BGP`, `Filter`, `LeftJoin`, `Union`, `Minus`, `ToMultiSet`, etc.). This algebra tree is what the planner actually reads.

3. **Walk the algebra tree** — The `walk()` function (from `lcquad_utils.py`) recursively visits every node in the algebra tree. Different visitors extract different things:
   - `BGP` nodes → basic graph pattern triples (subject, predicate, object)
   - `Filter` nodes → filter expressions
   - `LeftJoin` nodes → OPTIONAL triples
   - `Minus` nodes → MINUS clauses
   - `Union` nodes → UNION branches (flattened from rdflib's nested left-tree encoding)
   - `ToMultiSet` nodes → subqueries

4. **Classify each triple** — Each triple's predicate is inspected to determine what kind of operation it represents: entity lookup, type constraint, property traversal, Wikidata statement link, qualifier, etc. Each triple becomes one or more plan steps.

5. **Emit plan steps** — Steps are emitted for all SPARQL constructs: `find_entity`, `find_object`, `find_subjects`, `find_by_type`, `filter_type`, `find_statement`, `filter_statement`, `find_qualifier`, `property_path`, `optional_find`, `left_join`, `filter`, `exists`, `exclude`, `union`, `subquery`, `bind`, `seed_values`, `group_by`, `aggregate`, `having_filter`, `sort`, `limit`, `sort_and_limit`, `distinct`.

6. **Return the plan** — A dict with a `steps` list and `sparql_hints` (query form, whether filters are present).

**Example output:**

```json
{
  "steps": [
    {
      "step": 1,
      "action": "find_entity",
      "description": "locate Q30971 in the knowledge graph",
      "surface_form": "Q30971",
      "semantic_type": "ENTITY",
      "output_variable": "?q30971"
    },
    {
      "step": 2,
      "action": "find_statement",
      "description": "find P1082 statement of Q30971",
      "property": "P1082",
      "semantic_type": "PROPERTY",
      "subject_variable": "?q30971",
      "output_variable": "?s"
    },
    ...
  ],
  "sparql_hints": {
    "query_form": "SELECT",
    "has_filter": true
  }
}
```

---

### `lcquad_utils.py` — Utilities & Preprocessing

All supporting functions live here. `sparql_planner.py` imports from this file.

**rdflib tree helpers**

- `walk(node, visitor)` — Recursively traverses any rdflib algebra tree (`CompValue` nodes and lists). Every node gets passed to the visitor function. This is how all extraction (BGPs, filters, unions, etc.) is done.
- `collect_bgp_triples(alg)` — Walks the algebra tree and separates triples into regular, OPTIONAL, and MINUS buckets. Subquery triples are excluded so they don't get processed twice.
- `collect_filters(alg)` — Walks the tree collecting all `Filter` nodes, skipping aggregation-internal filters.
- `collect_union_branches(alg)` — rdflib encodes `UNION(A, B, C)` as a left-nested tree `Union(Union(A, B), C)`. This function flattens that into a plain list of branches, each with its own triples and filters.
- `parse_filter_expr(expr)` — Recursively interprets a filter expression `CompValue` into a typed dict (`numeric_filter`, `date_filter`, `string_filter`, `regex_filter`, `lang_filter`, `in_filter`, `exists`, `not_exists`, `compound`).

**Predicate classifiers**

These inspect a predicate URI and return True/False:

| Function | What it matches |
|---|---|
| `is_type_pred` | `rdf:type` or Wikidata's `wdt:P31` |
| `is_label_pred` | `rdfs:label` |
| `is_statement_link_pred` | `p:Pxxx` — links entity to a statement node |
| `is_qualifier_pred` | `pq:Pxxx` — retrieves a qualifier from a statement |
| `is_statement_value_pred` | `ps:Pxxx` — retrieves or constrains the value of a statement |

These classifiers drive the decision of which plan step action to emit for each triple.

**Other extractors**

- `extract_order_limit(alg)` — Finds `ORDER BY`, `LIMIT`, and sort direction from the algebra tree.
- `extract_group_by(alg, query_str)` — Extracts `GROUP BY` variables and `HAVING` clause.
- `extract_agg_info(query_str)` — Parses aggregation functions (`COUNT`, `MAX`, `MIN`, `SUM`, `AVG`) from the raw query string.
- `extract_bind(alg)` — Finds `BIND(expr AS ?var)` expressions.
- `extract_values(alg)` — Extracts `VALUES` blocks (inline data tables).
- `extract_subquery_plan(alg, question, owl)` — Recursively calls `build_plan()` on any nested `SELECT` found inside a `ToMultiSet` node.
- `has_union / has_minus / has_distinct / has_subquery` — Boolean checks by walking the algebra tree.

**URI utilities**

- `local_name(uri)` — Extracts the local part of a URI (after `#` or last `/`).
- `entity_surface(uri)` — Human-readable label from a URI, with underscores replaced by spaces.
- `safe_var(uri)` — Derives a stable SPARQL variable name from a URI, preserving underscores so `birth_place` and `birth_date` don't collapse to the same token.
- `is_entity_uri(node)` — Returns True if the node is a `URIRef` matching known entity prefixes (e.g. `wd:`, `dbr:`).

**OWLIndex**

Loads an OWL ontology file and builds lookup tables for `rdfs:range` and `rdfs:subClassOf`. Used to resolve the semantic type of a property's range (e.g., `Person`, `Place`, `Date`, `Number`).

**LC-QuAD preprocessing**

Supports both dataset versions, each targeting a different knowledge graph:

| Dataset | Knowledge Graph | Prefixes |
|---|---|---|
| LC-QuAD 1.0 | DBpedia | `dbo:`, `dbr:`, `dbp:` |
| LC-QuAD 2.0 | Wikidata | `wd:`, `wdt:`, `p:`, `ps:`, `pq:` |

- `normalize_lcquad_query(sparql)` — Cleans up common issues in LC-QuAD queries from both versions: fixes malformed prefixes, removes encoding artifacts, standardizes whitespace.
- `extract_lcquad2(entry)` — Extracts `uid`, `question`, and `sparql` from a single LC-QuAD 2.0 JSON record. For LC-QuAD 1.0 records the `build_training_dataset` function reads fields directly (`corrected_question`, `sparql_query`).
- `is_valid_training_sample(plan, ...)` — Quality filter for generated plans. Rejects plans that have zero graph traversal steps on a SELECT query, missing required fields, broken step numbering, broken variable flow, or (optionally) an empty result when the gold SPARQL is executed against a live endpoint.
- `build_training_dataset(lcquad_path, dataset="lcquad2", ...)` — Loads a LC-QuAD 1.0 or 2.0 JSON file, runs `build_plan()` on every entry, applies all quality filters, and returns a list of clean training records optionally written as JSON Lines. Pass `dataset="lcquad1"` for LC-QuAD 1.0.
- `visualize_plan(plan)` — Renders the plan as a DAG using [Graphviz](https://graphviz.org/), displayed inline in a Jupyter notebook.

---

## Installation

```bash
pip install rdflib graphviz
# graphviz system package also needed for visualization:
# Ubuntu/Debian: sudo apt install graphviz
# macOS:         brew install graphviz
```

---

## Quick Start

```python
from sparql_planner import build_plan

query = """
SELECT ?value WHERE {
  wd:Q30971 p:P1082 ?s .
  ?s ps:P1082 ?x FILTER(CONTAINS(?x, '223734.0')) .
  ?s pq:P585 ?value
}
"""

plan = build_plan(query)

import json
print(json.dumps(plan, indent=2))
```

**Building a training dataset — LC-QuAD 2.0 (Wikidata):**

```python
from lcquad_utils import build_training_dataset

records = build_training_dataset(
    lcquad2_path="lcquad2_train.json",
    output_path="train_plans.jsonl",
    dataset="lcquad2",          # Wikidata queries
)
# Each record: {"uid": ..., "question": ..., "sparql": ..., "plan": ...}
```

**Building a training dataset — LC-QuAD 1.0 (DBpedia):**

```python
records = build_training_dataset(
    lcquad2_path="lcquad1_train.json",
    output_path="train_plans_dbpedia.jsonl",
    dataset="lcquad1",          # DBpedia queries
)
```

---

## Supported SPARQL Features

- `SELECT` and `ASK` query forms
- Basic Graph Patterns (BGP)
- `OPTIONAL` / `LEFT JOIN`
- `UNION`
- `MINUS`
- `FILTER` — numeric, date, string, regex, lang, `IN`, `EXISTS`, `NOT EXISTS`, compound `AND`/`OR`
- `BIND`
- `VALUES`
- Nested `SELECT` (subqueries)
- Property paths
- `GROUP BY` / `HAVING`
- Aggregates: `COUNT`, `MAX`, `MIN`, `SUM`, `AVG`
- `ORDER BY`, `LIMIT`, `DISTINCT`
- Wikidata statement/qualifier pattern (`p:` / `ps:` / `pq:`)

---

## Dependencies

| Package | Purpose |
|---|---|
| `rdflib` | SPARQL parsing and algebra tree |
| `graphviz` | Plan visualization (optional) |
| `IPython` | Inline display in notebooks (optional) |

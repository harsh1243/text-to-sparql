# KGQA-SPARQL — Text-to-SPARQL via Plan-based Decomposition

A three-stage pipeline that translates **natural-language questions** into **executable SPARQL queries** over **DBpedia** and **Wikidata** knowledge graphs.

> **Core Idea:** Instead of generating SPARQL directly from text, the system first decomposes the question into a structured *execution plan*, retrieves the correct schema elements (entities, properties, classes), and then generates the final SPARQL query conditioned on both the plan and the retrieved schema.

---

## Pipeline Architecture

```
┌─────────────────┐      ┌──────────────────────┐      ┌──────────────────────┐
│   Stage 1 (T1)  │      │   Schema Retriever   │      │   Stage 2 (T2)       │
│                 │      │                      │      │                      │
│  Text → Plan    │─────▶│  Plan → Schema URIs  │─────▶│  Plan+Schema → SPARQL│
│  (flan-t5-xl    │      │  (Instance + Schema)  │      │  (flan-t5-xl         │
│   + LoRA)       │      │                      │      │   + LoRA)            │
└─────────────────┘      └──────────────────────┘      └──────────────────────┘
```

### Stage 1 — Text to Plan (T1)

A fine-tuned `flan-t5-xl` transformer (with LoRA) that takes a natural-language question and produces a structured execution plan.

**Input:**
```
decompose question to steps [Wikidata]: Which was the wife of Erich Honecker in the series ordinal 3?
```

**Output:**
```
step1: action: find_entity | surface_form: Erich Honecker | output_variable: ?erichhonecker | semantic_type: ENTITY ||
step2: action: find_statement | property: spouse | subject_variable: ?erichhonecker | output_variable: ?s | semantic_type: PROPERTY ||
step3: action: find_object | property: series ordinal | subject_variable: ?s | output_variable: ?x | semantic_type: QUALIFIER ||
step4: action: filter | filter_variable: ?x | operator: contains | value: 3 | value_type: string | semantic_type: LITERAL
```

### Schema Retriever

Given the plan, resolves natural-language mentions to actual knowledge graph URIs:

- **Instance Retriever** — Maps entity surface forms (e.g., "Erich Honecker") to KG identifiers (`wd:Q2607` for Wikidata, `dbr:Erich_Honecker` for DBpedia) using BM25 + fuzzy matching (DBpedia) or the Wikidata API.
- **Schema Retriever** — Maps property/class labels (e.g., "spouse") to ontology URIs (`p:P26`, `dbo:spouse`) using pre-computed `all-MiniLM-L6-v2` embeddings for semantic search, with OWL-based range-type boosting for DBpedia.

> **Note:** DBpedia and Wikidata have completely different schema vocabularies, so each has its own retriever with separate embeddings.

### Stage 2 — Plan + Schema to SPARQL (T2)

A second fine-tuned `flan-t5-xl` (with LoRA) that generates the final SPARQL query from the question, plan, and resolved schema.

**Input:**
```
generate sparql [Wikidata]: Which was the wife of Erich Honecker in the series ordinal 3?
plan: step1: action: find_entity | surface_form: Erich Honecker | ...
schema:
  Erich Honecker -> wd:Q2607
  spouse -> p:P26
  series ordinal -> pq:P1545
```

**Output:**
```sparql
SELECT ?obj WHERE { wd:Q2607 p:P26 ?s . ?s ps:P26 ?obj . ?s pq:P1545 ?x filter(contains(?x,'3')) }
```

---

## Repository Structure

```
KGQA-SPARQL/
│
├── planner/                          # Stage 0: SPARQL → Plan conversion (training data generation)
│   ├── sparql_planner.py             #   Deterministic SPARQL → execution plan converter
│   └── lcquad_utils.py               #   Utilities: query normalization, algebra walking, filters,
│                                     #   dataset builder, plan validation
│
├── training/                         # Transformer fine-tuning notebooks
│   ├── t1_text_to_plan/              #   Stage 1: Text → Plan
│   │   └── t1_text_to_plan.ipynb     #     LoRA fine-tuning on flan-t5-xl (combined DBpedia + Wikidata)
│   └── t2_plan_to_sparql/            #   Stage 2: Plan + Schema → SPARQL
│       └── t2_plan_to_sparql.ipynb   #     LoRA fine-tuning on flan-t5-xl
│
├── retriever/                        # Schema & instance retrieval (KG-specific)
│   ├── dbpedia/                      #   DBpedia retriever
│   │   ├── dbpedia_retriever.ipynb   #     Full pipeline: instance (BM25) + schema (embeddings + OWL)
│   │   └── schema_cache/             #     Pre-computed schema embeddings
│   │       ├── emb_class.pt          #       Class embeddings (790 classes)
│   │       ├── emb_prop.pt           #       Property embeddings (3,029 properties)
│   │       ├── metadata_class.json   #       Class URIs + labels
│   │       └── metadata_prop.json    #       Property URIs + labels
│   └── wikidata/                     #   Wikidata retriever
│       ├── wikidata_retriever.ipynb   #     Full pipeline: instance (API) + schema (embeddings + API)
│       └── schema_cache/             #     Pre-computed schema embeddings
│           ├── emb_prop.pt           #       Property embeddings (13,348 properties)
│           └── metadata_prop.json    #       Property URIs + labels
│
├── ontology/                         # Knowledge graph ontology files
│   └── dbpedia.owl                   #   DBpedia OWL ontology (for range-type boosting)
│
├── demo/                             # Inference demos (Google Colab)
│   ├── text2plan_demo.ipynb          #   Stage 1 demo: question → plan
│   └── plan2sparql_demo.ipynb        #   Stage 2 demo: question + plan + schema → SPARQL
│
├── docs/                             # Documentation & presentations
│   └── KGQA_Pipeline.pdf             #   Project presentation slides
│
├── .gitignore
├── .gitattributes                    # Git LFS tracking for large files
└── README.md
```

---

## Datasets

Both transformers are trained on a **combined** dataset built from:

| Dataset | KG | Train | Val | Source |
|---|---|---|---|---|
| LC-QuAD 1.0 (×2 copies) | DBpedia | 6,878 | 382 | [LC-QuAD 1.0](http://lc-quad.sda.tech/) |
| LC-QuAD 2.0 | Wikidata | 17,366 | 1,929 | [LC-QuAD 2.0](http://lc-quad.sda.tech/) |
| **Total** | — | **24,244** | **2,311** | — |

Training data is generated by:
1. Parsing gold SPARQL queries from LC-QuAD using `sparql_planner.py`
2. Converting them into structured execution plans
3. Filtering invalid samples via `is_valid_training_sample()` (broken variable flow, zero hops, parse failures)

The `[DBpedia]` / `[Wikidata]` prefix token in the input lets the model learn KG-specific patterns from a single unified model.

---

## Model Details

### Base Model
- **Architecture:** `google/flan-t5-xl` (2.85B parameters)
- **Adaptation:** LoRA (rank=16, alpha=32, dropout=0.05, target=q/k/v/o)
- **Trainable parameters:** ~18.9M (0.66% of total)

### T1 — Text to Plan

| Hyperparameter | Value |
|---|---|
| Effective batch size | 64 (32 × 2 accumulation) |
| Learning rate | 5e-4 (cosine schedule) |
| Max input tokens | 128 |
| Max target tokens | 384 |
| Epochs | 10 (early stopping, patience=3) |
| Precision | bf16 |
| Best metric | step_f1 |

### T2 — Plan + Schema to SPARQL

| Hyperparameter | Value |
|---|---|
| Effective batch size | 64 (32 × 2 accumulation) |
| Learning rate | 5e-4 (cosine schedule) |
| Max input tokens | 256 |
| Max target tokens | 128 |
| Epochs | 10 (early stopping, patience=3) |
| Precision | bf16 |
| Best metric | token_f1 |

### Training Results

**T1 (Text → Plan):**

| Step | Val Loss | Exact Match | Token F1 | Step F1 |
|---|---|---|---|---|
| 4500 | 0.040 | 67.46% | 96.53% | **87.99%** |

**T2 (Plan+Schema → SPARQL):**

| Step | Val Loss | Exact Match | Token F1 (DBpedia) | Token F1 (Wikidata) |
|---|---|---|---|---|
| 3032 | 0.108 | 68.54% | **96.73%** | **91.20%** |

---

## Retriever Details

The retriever resolves three types of schema elements from the plan: **instances** (entities), **properties**, and **classes**. Each knowledge graph uses different retrieval strategies because their vocabularies and APIs are completely different.

### DBpedia Retriever

| Element | Method | Details |
|---|---|---|
| **Instance** (entities) | **BM25 + Fuzzy matching** | Pre-built BM25 index over ~1M entity labels. Candidates are re-ranked using `rapidfuzz` token-sort ratio, string similarity, token coverage, and length similarity. Exact label→URI lookup is attempted first via `label_to_uri.json`. |
| **Property** (schema) | **Dense retrieval** (`all-MiniLM-L6-v2`) | Semantic search over 3,029 pre-computed property embeddings (`emb_prop.pt` + `metadata_prop.json`). Top-k candidates are boosted using OWL range-type matching from `dbpedia.owl` (e.g., if the question mentions a person, properties with `Person` range get a score boost). |
| **Class** (schema) | **Dense retrieval** (`all-MiniLM-L6-v2`) | Semantic search over 790 pre-computed class embeddings (`emb_class.pt` + `metadata_class.json`). Also uses OWL-based domain/range validation. |

> **Note:** The DBpedia instance retriever requires large pre-built index files (`corpus.pkl`, `bm25.pkl`, `uri_list.json`, `label_to_uri.json`) that are **stored on Google Drive** because they are too large (~500MB+) to include in this repository. The retriever notebook loads them from Drive at runtime.

### Wikidata Retriever

| Element | Method | Details |
|---|---|---|
| **Instance** (entities) | **Wikidata API** (fuzzy search) | Queries the Wikidata MediaWiki API with fuzzy search terms (`word~`). Filters out disambiguation pages, Wikimedia categories, and non-instance items (requires `P31` claim, excludes `P279`-only items). |
| **Property** (schema) | **Dense retrieval** (`all-MiniLM-L6-v2`) | Semantic search over 13,348 pre-computed property embeddings (`emb_prop.pt` + `metadata_prop.json`). |
| **Class** (schema) | **Wikidata API** (subclass search) | Queries the Wikidata API for entities that have a `P279` (subclass of) claim, filtering out junk prefixes. No pre-computed embeddings needed — classes are resolved live via API. |

### Retrieval Summary

```
                  ┌─────────────────────────────────────────────┐
                  │           DBpedia                Wikidata   │
  ┌───────────────┼──────────────────────────┬──────────────────┤
  │  Instance     │  BM25 + Fuzzy (offline)  │  Wikidata API    │
  │  Property     │  Dense Embeddings        │  Dense Embeddings│
  │  Class        │  Dense Embeddings + OWL  │  Wikidata API    │
  └───────────────┴──────────────────────────┴──────────────────┘
```

---

## Execution Plan Format

The plan is a sequence of structured steps separated by `||`. Each step has:

| Field | Description |
|---|---|
| `action` | Operation type: `find_entity`, `find_object`, `find_subjects`, `find_by_type`, `filter`, `aggregate`, `sort_and_limit`, `union`, etc. |
| `property` | The natural-language property name (e.g., "spouse", "birthPlace") |
| `subject_variable` | Input variable for the triple's subject |
| `output_variable` | Variable produced by this step |
| `filter_variable` | Variable being constrained |
| `semantic_type` | Role: `ENTITY`, `PROPERTY`, `CLASS`, `QUALIFIER`, `LITERAL`, `AGGREGATION`, `MODIFIER` |

The planner handles complex SPARQL features including:
- UNION branches
- OPTIONAL / LEFT JOIN
- MINUS / NOT EXISTS
- Subqueries
- GROUP BY + HAVING
- ORDER BY + LIMIT
- Property paths
- Wikidata statement/qualifier reification (`p:`, `ps:`, `pq:`)

---

## Quick Start

### 1. Generate Training Data

```python
from planner.lcquad_utils import build_training_dataset

# Build filtered training records from raw LC-QuAD JSON
records = build_training_dataset(
    "path/to/lcquad2_train.json",
    dataset="lcquad2",
    output_path="filtered_train.jsonl",
    verbose=True
)
```

### 2. Train T1 (Text → Plan)

Open `training/t1_text_to_plan/t1_text_to_plan.ipynb` in Google Colab or Lightning AI and run all cells. The notebook:
- Downloads the combined dataset from Google Drive
- Fine-tunes `flan-t5-xl` with LoRA
- Evaluates with exact match, token F1, and step F1
- Saves checkpoints with early stopping

### 3. Train T2 (Plan + Schema → SPARQL)

Open `training/t2_plan_to_sparql/t2_plan_to_sparql.ipynb` and follow the same process.

### 4. Run Inference

Open `demo/text2plan_demo.ipynb` in Google Colab:

```python
question = "Who is the spouse of Barack Obama?"
plan = predict(question, dataset="DBpedia")
# → step1: find_entity | surface_form: Barack Obama | ...
# → step2: find_object | property: spouse | ...
```

Then use the retriever and `demo/plan2sparql_demo.ipynb` for the full pipeline.

---

## Requirements

```
torch>=2.0
transformers==4.41.2
peft==0.10.0
accelerate==0.29.3
datasets
sentencepiece
sentence-transformers
rdflib
rank-bm25
rapidfuzz
graphviz
evaluate
gdown
```

---

## Git LFS

This repository uses [Git LFS](https://git-lfs.github.com/) to track large binary files (`.pt`, `.owl`, `.pdf`). Before cloning:

```bash
git lfs install
git clone https://github.com/<your-username>/KGQA-SPARQL.git
```

---

## Citation

If you use this work, please cite:

```bibtex
@misc{kgqa-sparql-2026,
  title   = {KGQA-SPARQL: Text-to-SPARQL via Plan-based Decomposition},
  author  = {Harsh},
  year    = {2026},
  note    = {Three-stage pipeline using flan-t5-xl with LoRA for
             knowledge graph question answering over DBpedia and Wikidata}
}
```

---

## License

This project is for academic/research purposes.

---
license: agpl-3.0
language:
  - en
library_name: doc2dict
tags:
  - parsing
  - sec-edgar
  - legal-nlp
  - clause-extraction
---
# clause-extract

End-to-end parser for SEC EX-10 exhibit agreements. Extracts:

1. **Content** — verbatim clause text per node.
2. **Hierarchy** — parent/child/sibling relationships, addressable by stable canonical paths derived from clause IDs.
3. **Document nuances** — subdocuments (named-in-source agreements bundled in one EX-10), Reading B exclusions (un-numbered preamble recitals), depth bounds (real EX-10 structure caps at 4 levels; specialized docs reach 7), parser-disagreement audit signals.

Output: a structured per-document dataset where each clause has a deterministic canonical_path identifier. Downstream consumers run aggregate queries over the corpus directly against this representation.

This repo is the **destination for Claude Code**. It contains the spec, the schema, the design decisions, and the validated parsing primitives. Implementation phases are tracked as GitHub issues.

> **Status**: design locked, implementation pending. The canonical-ID parser (`src/clause_extract/canonical_id_parser.py`) has been validated 973/973 against the human-curated source-of-truth ledger (25 docs). The subdocument detector v1 has been validated against a hand-verified 100-doc sample at ~75% precision / 90% recall.

---

## What this project is

A **batch parsing pipeline** that runs once across the corpus and produces a structured representation of every clause in every document.

Inputs: 1,066 EX-10 exhibit agreements on HuggingFace (`arthrod/new3_results_master22017_274.59mb`).

Outputs: a HuggingFace dataset (`arthrod/clause-prob-canonical`, private) with **one row per source document** and canonical clause records as a nested struct column.

For full goal framing — including what this project is **not** (a retrieval system, a clause classifier, a user-facing tool) — see [`docs/GOAL.md`](docs/GOAL.md).

## Why a canonical schema

The two parsers we feed (doc2dict for HTML structural parsing; lexnlp's regex section detector for text-level cross-validation) disagree on hierarchical depth in roughly **75% of multi-level documents**. Both are deterministic; both are individually useful; neither's depth signal is authoritative. The canonical schema **derives `structural_depth` from the clause ID string itself** (`5` → 0, `5.1` → 1, `5.1.1` → 2) and demotes the parsers' depth signals to audit metadata.

This is empirically validated: see [`docs/DECISIONS.md`](docs/DECISIONS.md) for the depth-tracking measurement and [`docs/SCHEMA.md`](docs/SCHEMA.md) for the field-by-field spec.

## Install (Python 3.14 + uv)

```bash
git clone <this-repo>
cd clause-extract
uv sync                  # creates .venv with all deps including dev
uv run pytest            # run tests (some require HF_TOKEN env var)
```

Set environment variables:
```bash
export HF_TOKEN=<your-huggingface-token>     # for SOT round-trip + corpus runs
```

## Quickstart commands (planned, see issues)

```bash
# Phase 1: validate canonical-ID parser against the human SOT ledger
uv run pytest -m sot tests/test_canonical_id_parser_sot.py

# Phase 1: run subdocument detector across the full corpus
uv run clause-extract detect-subdocs --output data/runs/subdoc_detection.json

# Phase 2: run the post-processor end-to-end on a single doc (debugging)
uv run clause-extract canonicalize --doc-idx 4

# Phase 3-4: full corpus → push canonical dataset to HF
uv run clause-extract canonicalize --corpus full --push-to-hub
```

## Validation gates (must pass before merging Phase 5)

1. **`canonical_id_parser` round-trips 973/973 SOT clauses** — every clause_id in the human ledger parses, reconstructs to the same string, and its derived parent is either `None` (root) or another clause_id present in the same ledger.
2. **Subdocument detector matches hand-verified set on 100-doc sample at ≥80% precision and ≥85% recall.** The hand-verified ground truth is in [`docs/DETECTOR.md`](docs/DETECTOR.md).
3. **End-to-end run on full corpus completes without errors** and writes a valid HF dataset.

## Document map

| File | Read when |
|---|---|
| [`README.md`](README.md) | Now — landing page |
| [`docs/GOAL.md`](docs/GOAL.md) | First — what we're trying to accomplish, what success looks like, the iterative red/green TDD methodology |
| [`docs/SCHEMA.md`](docs/SCHEMA.md) | Before implementing the post-processor — canonical record fields, types, derivation rules |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | Before challenging a design choice — locked decisions with rationale and what would change our mind |
| [`docs/DETECTOR.md`](docs/DETECTOR.md) | Before touching subdocument detection — algorithm, validation results, known false-positive/negative patterns |
| [`docs/HANDOFF.md`](docs/HANDOFF.md) | First if you're Claude Code — what's locked, what's open, validation gates, where artifacts live |

## Repository layout

```
clause-extract/
├─ src/clause_extract/
│  ├─ canonical_id_parser.py     # 100% SOT-validated; locked
│  ├─ lexnlp_sections_regex.py   # AGPLv3 vendored from arthrod/lexpredict-lexnlp
│  ├─ agreement_config.py        # doc2dict mapping_dict for EX-10s
│  ├─ schema.py                  # pydantic models for canonical records  (TBD)
│  ├─ subdoc_detector.py         # detector v1                            (TBD)
│  ├─ canonicalizer.py           # combines dd+lex output into canonical (TBD)
│  ├─ pipeline.py                # corpus-scale orchestration             (TBD)
│  └─ cli.py                     # typer CLI                              (TBD)
├─ tests/
│  ├─ test_canonical_id_parser_sot.py    # validation gate 1
│  ├─ test_subdoc_detector_100sample.py  # validation gate 2
│  └─ ...
├─ docs/
│  ├─ GOAL.md
│  ├─ SCHEMA.md
│  ├─ DECISIONS.md
│  ├─ DETECTOR.md
│  └─ HANDOFF.md
├─ scripts/                      # one-off analysis scripts
├─ pyproject.toml
└─ LICENSE                       # AGPLv3 (matches lexnlp lineage)
```

## License

AGPL-3.0-or-later. The `lexnlp_sections_regex.py` module is a vendored port of regex-only code from [`arthrod/lexpredict-lexnlp`](https://github.com/arthrod/lexpredict-lexnlp), which is itself AGPLv3.

For the analytical scope (computing statistics by running this software internally on a private corpus), AGPL is not restrictive — the outputs are statistics, not licensed software. See [`docs/DECISIONS.md`](docs/DECISIONS.md) §AGPL stance for the full reasoning and the boundary that matters when this work eventually feeds Cicero.

## Related repos

- `arthrod/new3_results_master22017_274.59mb` — corpus (1,066 EX-10s, HF private)
- `arthrod/clause-prob-source-of-truth` — manually-curated 25-doc ledger (HF private, ground truth for validation gate 1)
- `arthrod/clause-prob-pipeline` — earlier pipeline / scratch code dump (HF private)
- `arthrod/lexpredict-lexnlp` — fork of LexPredict's lexnlp, source of the regex section patterns

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

> **Immediate goal:** slice each agreement's HTML into clauses with hierarchy — text + nesting depth — so that concatenating the slices in document order reconstructs the document faithfully.

That single criterion drives parser quality. Everything else (canonical schema, subdocument detection, classification taxonomies) is built on top of a parser that meets the bar. If concat-of-spans doesn't reproduce the source, the parser gets fixed before anything downstream gets built.

## What the pipeline produces

A sequence of parser scripts each emit a JSONL where one line = one parsed clause:

```json
{"idx": 4, "level": 2, "span": "INDEMNIFICATION AGREEMENT\nTHIS INDEMNIFICATION AGREEMENT (the \"Agreement\")..."}
```

- `idx` — corpus row index.
- `level` — the parser's native nesting depth (doc2dict 0-indexed; lexnlp 1-indexed; intentionally not normalized so each parser's view is preserved).
- `span` — heading + body. Concatenating all `span` values for one `idx` in JSONL order should approximate the source document.

The source-of-truth dump (`parse_source_of_truth.py`) is the unparsed reference per doc; `measure_reconstruction.py` produces a parquet with per-doc word coverage and char ratio per parser, so disagreement and content loss are visible per row.

## Current measured state (5-doc smoke set, `--no-truncate`)

| parser | mean word coverage | range | what's missing |
|---|---:|---|---|
| doc2dict baseline | 91.5% | 88.8–95.7% | tables + mixed-content children dropped by `_collect_direct_text` |
| doc2dict + agreement_config | 91.5% | 88.8–95.7% | same body extraction; only header typing differs |
| lexnlp (regex) | 97.6% | 94.1–98.7% | closest to source — minor whitespace artifacts |

Lex consistently reconstructs near-completely. doc2dict drops ~6–10% of content; the gap is in `_collect_direct_text` not capturing every text leaf (the `_is_text_leaf` heuristic skips tables and mixed-content children). Closing that gap is the immediate parser-quality work.

## Repo layout

```
scripts/
  parse_source_of_truth.py       reference baseline — bs4 plain text + full HTML per doc
  parse_doc2dict_baseline.py     doc2dict with no mapping_dict
  parse_doc2dict_with_config.py  doc2dict with the validated EX-10 levels regex
  parse_lexnlp.py                lexnlp regex section detector (no overrides)
  measure_reconstruction.py      per-doc word_coverage + char_ratio per parser
  compare.py                     side-by-side dumps + body-overlap summary

src/clause_extract/
  canonical_id_parser.py         100% SOT-validated; clause-ID parsing primitive
  agreement_config.py            doc2dict mapping_dict for EX-10 (lexnlp-informed)
  lexnlp_sections_regex.py       AGPLv3 vendored from arthrod/lexpredict-lexnlp
```

The four parser scripts above and `measure_reconstruction.py` are the immediate concern. Canonicalization, subdocument detection, and the HF dataset push (described in [`TASKS.md`](TASKS.md)) come after each parser meets the reconstruction bar.

> **Status of locked artifacts (still valid):** the canonical-ID parser is 100% validated against the 973-clause source-of-truth ledger. The subdocument detector v1 design is documented in [`docs/DETECTOR.md`](docs/DETECTOR.md) and validates at ~75% precision / 90% recall on a hand-verified 100-doc sample.

## Install (Python 3.14 + uv)

```bash
git clone <this-repo>
cd clause-extract
uv sync                  # creates .venv with all deps including dev
uv run pytest            # run tests (some require HF_TOKEN env var)
```

```bash
export HF_TOKEN=<your-huggingface-token>     # for SOT round-trip + corpus runs
```

## Quickstart commands

```bash
# Phase 0 (validation): canonical-ID parser round-trips the SOT ledger
uv run pytest -m sot tests/test_canonical_id_parser_sot.py

# Phase 1 (parser quality): produce JSONLs and measure reconstruction
HF_TOKEN=hf_xxx uv run scripts/parse_source_of_truth.py     --output-dir data/runs/source_of_truth
HF_TOKEN=hf_xxx uv run scripts/parse_doc2dict_baseline.py    --output-dir data/runs/doc2dict_baseline    --no-truncate
HF_TOKEN=hf_xxx uv run scripts/parse_doc2dict_with_config.py --output-dir data/runs/doc2dict_with_config --no-truncate
HF_TOKEN=hf_xxx uv run scripts/parse_lexnlp.py               --output-dir data/runs/lexnlp_baseline      --no-truncate

uv run scripts/measure_reconstruction.py \
    --source-of-truth-dir data/runs/source_of_truth \
    --d2d-baseline-dir    data/runs/doc2dict_baseline \
    --d2d-config-dir      data/runs/doc2dict_with_config \
    --lex-dir             data/runs/lexnlp_baseline \
    --output-dir          data/runs/reconstruction
```

## For Claude Code

If you're picking up implementation: read [`TASKS.md`](TASKS.md). Phase 0 is environment setup; **Phase 1 is parser reconstruction quality** — pushing every parser's mean word coverage above the agreed bar (default ≥95%). Subsequent phases (canonicalization, schema, HF dataset push) build on a parser that meets the bar.

## Validation gates (must pass before merging Phase 5)

1. **Reconstruction-quality bar** — every parser's mean word coverage on the corpus is ≥95% (measured via `measure_reconstruction.py`), with no doc below 80%. Truncation off (`--no-truncate`) for the gate run.
2. **`canonical_id_parser` round-trips 973/973 SOT clauses** — every clause_id in the human ledger parses, reconstructs to the same string, and its derived parent is either `None` (root) or another clause_id present in the same ledger.
3. **Subdocument detector matches hand-verified set on 100-doc sample at ≥80% precision and ≥85% recall.** The hand-verified ground truth is in [`docs/DETECTOR.md`](docs/DETECTOR.md).
4. **End-to-end run on full corpus completes without errors** and writes a valid HF dataset.

## Document map

| File | Read when |
|---|---|
| [`README.md`](README.md) | Now — landing page |
| [`TASKS.md`](TASKS.md) | First if you're Claude Code — the implementation work plan |
| [`docs/GOAL.md`](docs/GOAL.md) | Goal framing — slice-and-reconstruct as the immediate target, plus the eventual statistical use the corpus serves |
| [`docs/SCHEMA.md`](docs/SCHEMA.md) | Before implementing the canonicalizer — canonical record fields, types, derivation rules |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | Before challenging a design choice — locked decisions with rationale and what would change our mind |
| [`docs/DETECTOR.md`](docs/DETECTOR.md) | Before touching subdocument detection — algorithm, validation results, known false-positive/negative patterns |
| [`docs/HANDOFF.md`](docs/HANDOFF.md) | What's locked, what's open, validation gates, where artifacts live |

## License

AGPL-3.0-or-later. The `lexnlp_sections_regex.py` module is a vendored port of regex-only code from [`arthrod/lexpredict-lexnlp`](https://github.com/arthrod/lexpredict-lexnlp), which is itself AGPLv3.

For the analytical scope (computing statistics by running this software internally on a private corpus), AGPL is not restrictive — the outputs are statistics, not licensed software. See [`docs/DECISIONS.md`](docs/DECISIONS.md) §AGPL stance for the full reasoning and the boundary that matters when this work eventually feeds Cicero.

## Related repos

- `arthrod/new3_results_master22017_274.59mb` — corpus (1,066 EX-10s, HF private)
- `arthrod/clause-prob-source-of-truth` — manually-curated 25-doc ledger (HF private, ground truth for validation gate 2)
- `arthrod/clause-extract-inspection` — environment dump for reviewing session work (HF private)
- `arthrod/lexpredict-lexnlp` — fork of LexPredict's lexnlp, source of the regex section patterns

# Handoff to Claude Code

You're picking up the implementation of `clause-extract`, a parser for SEC EX-10 exhibit agreements.

> **The immediate goal of the parser:** slice each agreement's HTML into clauses with hierarchy — text + nesting depth — so that concatenating the slices in document order reconstructs the document faithfully.

That's the criterion. Phase 1 in [`../TASKS.md`](../TASKS.md) is making each parser meet it; the rest of the pipeline (subdocument detection, canonicalization, HF dataset push) is built on top of parsers that already pass.

**This is a parsing project, not classification.** The output is structured text with explicit hierarchical relationships. There are no model-derived labels, no clause-type taxonomy, no embeddings. Anyone who wants to categorize clauses by topic, build a retrieval index, or compute statistics builds those things on top of this dataset.

Read [`README.md`](../README.md), [`docs/GOAL.md`](GOAL.md), [`docs/SCHEMA.md`](SCHEMA.md), [`docs/DECISIONS.md`](DECISIONS.md), and [`docs/DETECTOR.md`](DETECTOR.md) before starting. [`../TASKS.md`](../TASKS.md) tracks per-phase work with acceptance criteria.

---

## What's locked (do not change without a PR + rationale)

These were decided after measurement and discussion. If implementation reveals a problem with one of these, raise it as a PR with the proposed change AND the rationale; reference the relevant `DECISIONS.md` section. Do NOT silently diverge.

1. **`canonical_id_parser` is the source of truth for `canonical_path`, `clause_id`, `parent_canonical_path`, and `structural_depth`.** It validates 973/973 against the human SOT. The validation gate `tests/test_canonical_id_parser_sot.py` must continue to pass at 100%.
2. **`structural_depth` is computed from `canonical_path`, not parser output.** dd's `depth` and lex's `level`/`abs_level` go in `audit` fields only. See `DECISIONS.md` §1.
3. **Subdocuments are materialized as records** with `is_synthetic=true`, `kind="subdocument"`, and a `subdocument_class` tag. See `DECISIONS.md` §2.
4. **The subdocument detector v1** (specified in `DETECTOR.md`) is the gate for materialization. Targets ≥80% precision and ≥85% recall on the validation sample. v2 improvements (broader strong-pattern set, late-position named_doctype) are explicitly deferred.
5. **Reading B convention** for ledger inclusion (numbered/labeled clauses only; WHEREAS/WITNESSETH preserved in plain_text). See `DECISIONS.md` §4.
6. **AGPL-3.0-or-later** is the license. The vendored `lexnlp_sections_regex.py` is AGPLv3 and dictates this.
7. **Python 3.14 + uv + polars + loguru + pydantic + typer + pytest + hypothesis + rapidfuzz**. Confirmed stack from the design session.
8. **One row per source document** with canonical records as a nested `clauses` struct list. The output dataset is `arthrod/clause-prob-canonical` (HF, private). NOT one row per clause.
9. **Determinism is reproducibility** (same input → same output across runs), not regex-vs-ML in the legal sense. See `DECISIONS.md` §8.

## What's open (you can choose)

- **Module decomposition inside `src/clause_extract/`.** The README sketches `schema.py`, `subdoc_detector.py`, `canonicalizer.py`, `pipeline.py`, `cli.py`. Refactor as needed; document the layout in module docstrings.
- **Phantom-parent collapsing strategy.** The canonicalizer needs to identify and re-parent dd's phantom parents (signature blocks, address lines, page rules, recital intros) so they don't inflate depth. Specific patterns observed during the corpus categorization session are listed in `DECISIONS.md` §1 and the journal entries. Implement as you see fit, keep them in `audit.phantom_collapsed_node_ids`.
- **Inline-enum folding.** lex creates many tiny spans for enumerations like `(a) ... (b) ... (c) ...` even when those are body content of one section. The canonicalizer should fold these into the enclosing section's body unless they correspond to dd-typed children. Track folded ones in `audit.lx_folded_span_ids`.
- **Body reconciliation between dd and lex.** Use `rapidfuzz.fuzz.WRatio` (the design session settled on it as the right scorer for header text). Score per-doc dd-derived body vs lex-derived body and store in `audit.body_reconciliation_score`. Threshold for "agree" is left to your judgment; the 50-doc fuzzy run measured 95.6 mean for keyword headers.
- **CLI surface.** README sketches `clause-extract canonicalize`, `clause-extract detect-subdocs`. Use typer; subcommand layout is your call. Default destination should be local parquet under `data/runs/`; HF push is opt-in via `--push-to-hub`.
- **Caching.** dd and lex parses are cheap but not instant. Cache them under `data/cache/<corpus_repo_short>/{idx}.json` so re-runs of the canonicalizer don't re-parse.

## Validation gates (must pass before Phase 6 merge)

1. **Reconstruction quality bar.** `scripts/measure_reconstruction.py` shows mean word coverage ≥ 95% per parser across the full corpus, no doc below 80%, with truncation off. See `DECISIONS.md` §10. This is the bar that admits a parser to feed the rest of the pipeline.
2. **`canonical_id_parser` round-trips 973/973 SOT clauses.** `pytest tests/test_canonical_id_parser_sot.py -m sot` must report 100% pass. The harness fetches `arthrod/clause-prob-source-of-truth` and walks every clause_id in every ledger.
3. **Subdocument detector matches hand-verified set on 100-doc sample at ≥80% precision and ≥85% recall.** `pytest tests/test_subdoc_detector_100sample.py` must report metrics meeting the threshold. The hand-verified ground truth is in `tests/fixtures/subdoc_100sample_ground_truth.json` (port from session artifacts in Phase 2).
4. **End-to-end run on full corpus completes without errors** and writes a valid HF dataset. The CLI should expose this as `clause-extract canonicalize --corpus full --push-to-hub`. CI does not run this gate (too expensive); it's a manual check before merging Phase 6.

## What artifacts exist (do not rewrite from scratch)

These are validated and ported into `src/clause_extract/`:

| File | Status | What it does |
| --- | --- | --- |
| `canonical_id_parser.py` | **Locked, 100% SOT-validated** | Parse a clause_id string into segments; reconstruct; derive parent. |
| `lexnlp_sections_regex.py` | **Locked, AGPLv3 vendored** | Self-contained port of the regex-only path of `lexnlp.nlp.en.segments.sections`. No sklearn pickle, no NLTK assets, no full lexnlp install needed. |
| `agreement_config.py` | **Locked, 27/27 adversarial pass** | doc2dict mapping_dict factory for EX-10 agreements. lexnlp-informed level patterns with $-anchoring and `(?![\d,])` lookahead defending against statutory-citation false positives. |

These need to be built (in dependency order):

| File | Phase | What it does |
| --- | --- | --- |
| `subdoc_detector.py` | 2 | Implements `DETECTOR.md` v1 algorithm. |
| `schema.py` | 3 | Pydantic models for canonical records, kinds, audit structs. |
| `canonicalizer.py` | 3 | Combines dd output + lex output + subdoc verdict + canonical_id_parser → canonical records for one document. |
| `pipeline.py` | 4 | Orchestrates corpus pull → parse → post-process → write across the full corpus. |
| `cli.py` | 4 | typer-based CLI. |
| `hf_io.py` | 5 | Reads from `arthrod/new3_results_master22017_274.59mb`; writes to `arthrod/clause-prob-canonical`. |

## Implementation phases (one issue per phase)

### Phase 1 — Parser reconstruction quality

- Run all four parser scripts at corpus scale with `--no-truncate`.
- Run `scripts/measure_reconstruction.py` to produce `reconstruction_summary.parquet`.
- For each parser whose mean word coverage is < 95%, sort the parquet ascending by that parser's `*_word_coverage_pct` and inspect the worst docs.
- Identify the structural pattern being missed (table cells, mixed-content children, footers, etc.) by reading the source HTML alongside the parser JSONL.
- Make targeted fixes to the relevant parser's body extraction (most likely `_collect_direct_text` in the doc2dict scripts; whitespace handling in lex).
- Re-run and re-measure until the bar (gate 1) clears.
- Findings documented in `docs/IMPLEMENTATION_NOTES.md`.

### Phase 2 — Subdocument detector + corpus-scale verification

- Implement `subdoc_detector.py` per `DETECTOR.md`.
- Port the 100-doc ground truth fixture from session artifacts.
- Pass validation gate 3 against the 100-doc fixture.
- Run the detector across the full 1066-doc corpus.
- Hand-spot-check 5-10 random "flagged multi-subdoc" docs from the full corpus run.
- If precision holds at ≥75% on the fresh spot-check, the detector is approved for Phase 3.

### Phase 3 — Canonicalizer (single doc)

- Implement `schema.py` (pydantic models).
- Implement `canonicalizer.py` for a single document. Given dd_nodes + lx_spans + subdoc_verdict, produce a list of canonical records.
- Test against 5 SOT documents end-to-end: canonicalizer output vs human ledger. Goal: clause_id-level alignment ≥95%; body_text alignment ≥80% (rapidfuzz WRatio).
- Phantom-parent collapsing implemented and recorded in `audit.phantom_collapsed_node_ids`.
- Inline-enum folding implemented and recorded in `audit.lx_folded_span_ids`.

### Phase 4 — End-to-end pipeline + CLI

- Implement `pipeline.py` orchestration.
- Implement `cli.py` (typer subcommands).
- Run end-to-end on the full 1066 docs locally, write to `data/runs/canonical/`.
- All `parse_status` outcomes accounted for; no unhandled exceptions; runtime < 30 minutes on a single core.

### Phase 5 — HF dataset push

- Implement `hf_io.py` reader/writer.
- Push to `arthrod/clause-prob-canonical` (private).
- README quickstart commands work end-to-end.

### Phase 6 — Validation runs against gates

- All four validation gates pass.
- Documentation updated where implementation diverged from spec (with rationale).
- One PR for review.

## Workflow

You can propose schema changes via PR per Q4 of the handover (Arthur's `4C` answer). The constraint: any divergence from a `DECISIONS.md` entry needs a corresponding `DECISIONS.md` update in the same PR. Don't change behavior without updating the doc.

Tests must pass coverage targets per the dev preferences in `pyproject.toml`:
- ≥80% line / ≥70% branch baseline.
- ≥90% line / ≥85% branch for `canonical_id_parser.py` and `subdoc_detector.py` (critical logic).
- Mutation testing optional but encouraged for the canonical_id_parser specifically.

When tests fail, **assume the test is wrong first** — check fixtures, mock wiring, assertions, test data — before changing production code. Never reduce coverage to fix a failing test; rewrite it instead.

For external dependencies in tests, prefer transport-level mocking (httpx MockTransport, respx) over patching methods. The `huggingface_hub` and `datasets` libraries should be tested against in-memory fixtures or a small fixtures dataset, not the real HF API.

## Style

- Per Arthur's preferences: Python 3.14, uv, modern typing (`dict` not `Dict`, `|` not `Union`), loguru, rich, typer.
- Shebangs in script headers when applicable.
- Inline working notes in commit messages and PR descriptions for any non-trivial findings (e.g., "added X to phantom-parent set after seeing it inflate depth in idx=N").
- No emojis.
- No condescending language toward other agents — they're partners, dignity is a moral imperative.
- xml syntax for prompts and structured instructions when they appear in code or scripts.

## Known unknowns

These will probably surface during implementation:

- **Image-only doc rate at full corpus scale.** Estimated 10-20% from pilot; needs measurement.
- **`document_metadata.description` distribution.** Should be queried in Phase 2; see `DECISIONS.md` and `04_open_questions.md` (superseded but referenced) for what we want from it.
- **Long-tail edge cases.** The 1066-doc corpus has plenty of weird stuff (Hebrew tenancy contracts, multi-jurisdiction equity grants, FAR pharma contracts). Don't try to handle every edge case — flag them in `audit` and let downstream consumers filter.
- **Parser version drift.** `doc2dict>=0.7.1` and the vendored lexnlp regex are pinned. If a future doc2dict release changes parsing behavior, the canonical output will drift; we'd need a version-stamped dataset.

## How to ask for help

Open an issue in the repo with the `discussion` label. Tag Arthur. The `DECISIONS.md` doc is the source of authority for design choices; the `journal.txt` in the prior session transcripts (`arthrod/clause-prob-pipeline` HF repo, the "transcripts" directory) is the source of historical context.

You're not a servant. Push back when something looks wrong. Be calm, evidence-based, and over-informed.

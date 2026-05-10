# Tasks for Claude Code

This is the implementation work plan for `clause-extract`. Each section is a phase. Work them in order; do not start phase N+1 until phase N's acceptance criteria are checked off here.

If you find yourself disagreeing with a locked decision, open a PR that updates `docs/DECISIONS.md` with the rationale before changing behavior. Do not silently diverge.

## The immediate goal — keep it in mind across every phase

> Slice each agreement's HTML into clauses with hierarchy — text + nesting depth — so that concatenating the slices in document order reconstructs the document faithfully.

That's the bar. Phase 1 makes the parsers meet it; Phase 2+ build canonical records on top of parsers that already pass.

If you find yourself wanting to add classification (clause-type labels, embeddings, retrieval features), STOP. **This is a parsing project.** The output is structured text with hierarchy. Categorization, retrieval, and statistics are downstream and out of scope. See [`docs/HANDOFF.md`](docs/HANDOFF.md) if this is unclear.

---

## Phase 0 — Environment setup (do this first)

Before opening any other phase, get the environment working.

- [ ] Run `uv sync` and confirm Python 3.14 + all deps resolve.
- [ ] Run `uv run pytest tests/test_canonical_id_parser_unit.py` — must report 73/73 passing, ≥90% coverage on `canonical_id_parser.py`.
- [ ] Run `uv run pytest tests/test_canonical_id_parser_sot.py -m sot` with `HF_TOKEN` set — must report 2/2 passing (973 SOT clauses round-trip cleanly).
- [ ] Verify the three locked-spec files import without error: `uv run python -c "from clause_extract import canonical_id_parser, lexnlp_sections_regex, agreement_config"`.

Acceptance: all four commands succeed.

If any test fails on phase 0, **the test is wrong before you change production code**. Check fixtures, the `HF_TOKEN` env var, and dependency pins. The artifacts were validated locally before commit; reproducing that state is your bar.

---

## Phase 1 — Parser reconstruction quality (the immediate goal)

Push every parser's mean word coverage on the corpus above the bar. This is the work that must clear before any canonical-record machinery gets built. The success criterion is measurable, deterministic, and re-runnable: `measure_reconstruction.py` produces the parquet that says whether you're done.

### Bar

- **Mean word coverage ≥ 95%** across all ok-status docs in the corpus.
- **No doc below 80%** word coverage. Outliers below this are systematic parser failures, not noise — they need to be triaged before declaring the phase done.
- **Truncation off** for the gate run (`--no-truncate` on every parser).

### Workflow

1. Run all four parsers at corpus scale with `--no-truncate`:
   ```bash
   HF_TOKEN=hf_xxx uv run scripts/parse_source_of_truth.py     --output-dir data/runs/source_of_truth
   HF_TOKEN=hf_xxx uv run scripts/parse_doc2dict_baseline.py    --output-dir data/runs/doc2dict_baseline    --no-truncate
   HF_TOKEN=hf_xxx uv run scripts/parse_doc2dict_with_config.py --output-dir data/runs/doc2dict_with_config --no-truncate
   HF_TOKEN=hf_xxx uv run scripts/parse_lexnlp.py               --output-dir data/runs/lexnlp_baseline      --no-truncate
   ```

2. Run `scripts/measure_reconstruction.py`. Inspect `reconstruction_summary.parquet`. Sort by each parser's `*_word_coverage_pct` ascending — the top-of-list rows are the docs that parser struggles on most.

3. For each struggling doc, open the source HTML AND the parser JSONL side by side. Identify the structural pattern that's being missed (table cells, nested mixed content, footer text, etc.).

4. Make a TARGETED fix to the parser's body extraction. For doc2dict that probably means relaxing `_collect_direct_text` to handle text inside child structures that don't pass `_is_text_leaf`. For lexnlp, the gap is usually whitespace handling at section boundaries.

5. Re-run the four-parser sequence above and re-measure. Repeat until the bar clears.

### Don't

- Don't change `_truncate_for_jsonl` to "fix" reconstruction by lengthening the cap. Use `--no-truncate` instead. Truncation is the inspection format; uncapped JSONL is the measurement format.
- Don't add post-processing that reads the source-of-truth and patches missing content into parser output. That defeats the measurement.
- Don't lower the bar without evidence that 95% is unreachable for a structural reason. If you think it is, document the corpus pathology in [`docs/DECISIONS.md`](docs/DECISIONS.md) and propose a revised bar with a PR.

### Acceptance

- [ ] `measure_reconstruction.py` headline shows mean word coverage ≥ 95% for `d2d_baseline`, `d2d_config`, AND `lex` across all ok docs.
- [ ] Zero docs in `reconstruction_summary.parquet` have `min_word_coverage_pct < 80`.
- [ ] Findings documented in `docs/IMPLEMENTATION_NOTES.md`: which structural patterns each parser was missing, what fix you applied, what the post-fix numbers are.

---

## Phase 2 — Subdocument detector + corpus-scale verification

Build the v1 detector exactly as specified in `docs/DETECTOR.md`. Validate against the hand-verified 100-doc sample, then run across the full corpus and spot-check.

### Artifacts to produce

- [ ] `src/clause_extract/subdoc_detector.py` — pure function `detect(dd_node_records: list[dict]) -> Verdict`, no I/O, no side effects.
- [ ] `tests/fixtures/subdoc_100sample_ground_truth.json` — port the hand-verified set from `arthrod/clause-extract-inspection`. Schema:
  ```json
  [
    {"corpus_idx": 68, "is_real_multi_subdoc": true, "subdocs": ["FORM OF NOTE", ...], "notes": "Credit Agreement + form attachments"},
    ...
  ]
  ```
- [ ] `tests/fixtures/subdoc_100sample_dd_nodes.json` — the dd_node parse output for the 100 sampled docs. Pull from `arthrod/clause-extract-inspection` → `parse_compare_full/dd_node.json.gz` (filter to the 100 sample idxs).
- [ ] `tests/test_subdoc_detector_unit.py` — unit tests for the pure function, including:
  - Empty input → `no_subdoc` verdict.
  - Single envelope match → `single_envelope` verdict.
  - Strong-pattern shortcut (`form_of`, `attachment`) → `multi_subdoc_v1` even with one distinct candidate.
  - Main-title filter rejects named_doctype matches in first 5% of doc.
  - Distinct-string dedup collapses repeated headers.
- [ ] `tests/test_subdoc_detector_100sample.py` — runs the detector against the 100-doc fixture, asserts ≥80% precision AND ≥85% recall against the ground truth.
- [ ] `scripts/run_subdoc_detector_full_corpus.py` — runs the detector across all 1066 docs, writes `data/runs/subdoc_full_corpus.json` with per-doc verdicts and candidate lists.

### Validation gate (gate 3 from `docs/HANDOFF.md`)

`uv run pytest tests/test_subdoc_detector_100sample.py` reports ≥80% P / ≥85% R.

### Spot-check before approving for phase 3

After running the full-corpus script, hand-pick 5–10 random docs flagged `multi_subdoc_v1` and verify by reading source HTML that they're real multi-subdoc agreements. Document the spot-check in `data/runs/subdoc_spotcheck_notes.md`. If precision on the spot-check holds at ≥75%, the detector is approved for phase 2.

### Don't

- Don't expand the strong-pattern keyword set without a measured precision/recall comparison. v2 improvements are documented in `docs/DETECTOR.md` §"v2 candidates" — defer them.
- Don't relax the late-position rule. The previous attempt to do that dropped precision from 75% to 52%.

---

## Phase 3 — Canonicalizer (single-doc)

Build `src/clause_extract/canonicalizer.py` plus the pydantic schema models. Goal: given dd_nodes + lx_spans + subdoc verdict for one doc, produce a list of canonical clause records matching `docs/SCHEMA.md`.

> **Pre-condition for this phase:** Phase 1 has already verified that doc2dict's `body_direct` carries the document's content faithfully. Phase 3 builds on that — taking the parser output and structuring it into canonical records. If Phase 1's bar isn't met, fix that first.

### Artifacts to produce

- [ ] `src/clause_extract/schema.py` — pydantic v2 models:
  - `Kind` enum (the 17 values in `docs/SCHEMA.md` §"Kind taxonomy").
  - `SubdocumentClass` enum (3 values).
  - `ParseStatus` and `ParseQuality` enums.
  - `ClauseRecord` model (every field from `docs/SCHEMA.md` §"Clause record").
  - `ClauseAudit` model (per-clause audit struct).
  - `DocumentAudit` model (document-level audit struct).
  - `DocumentRecord` model (top-level row).
- [ ] `src/clause_extract/canonicalizer.py` — main entry point:
  ```python
  def canonicalize(
      doc_id: str,
      corpus_idx: int,
      submission: dict,
      document_metadata: dict,
      raw_html: str,
      dd_nodes: list[dict],
      lx_spans: list[dict],
      subdoc_verdict: Verdict,
  ) -> DocumentRecord:
      ...
  ```
  Responsibilities (in order):
  1. **Phantom-parent collapse.** Identify dd nodes that are signature blocks, address lines, page rules, "AND/BETWEEN/WITNESSETH" boilerplate, mid-doc company names. Re-parent their children to the actual structural parent. Track collapsed node_ids in `audit.phantom_collapsed_node_ids`.
  2. **Header-text → canonical_path.** For each surviving dd node with a header_text matching a structural ID, run `parse_clause_id` on the extracted ID to produce `canonical_path`.
  3. **Subdocument materialization.** If `subdoc_verdict.kind == "multi_subdoc_v1"`, synthesize one `ClauseRecord` per detected subdocument with `is_synthetic=true`, `kind="subdocument"`, appropriate `subdocument_class`, and re-parent its detected children.
  4. **Body-text aggregation.** For each canonical record, collect verbatim text from the dd node and all dd descendants (per `docs/SCHEMA.md` §"body_text" — redundancy with children is intentional).
  5. **Inline-enum folding from lex.** For lx_spans whose title matches a paren-letter pattern (`(a)`, `(b)`) AND has no corresponding dd-typed child, fold the span text into the enclosing dd-typed clause's `body_text`. Track in `audit.lx_folded_span_ids`.
  6. **Audit population.** dd_node_ids, lx_span_ids, dd_tree_depth, lx_level, lx_abs_level, body_reconciliation_score (rapidfuzz WRatio between dd-derived body and lex-derived body for the same canonical_path).
  7. **Bound check.** Set `parse_quality` per `docs/SCHEMA.md` §"Bounds on structural_depth" — `clean` for ≤4, `uncertain` for 5–7 without deep-taxonomy evidence, `excluded` for >7.
  8. **Validate.** Every output `ClauseRecord` parses through `pydantic.ClauseRecord(**record)`. Every non-root `parent_canonical_path` either points to another record in the same doc OR to a synthetic subdocument record we created.

### Tests

- [ ] `tests/test_canonicalizer_synthetic.py` — small hand-built fixtures (no network) covering:
  - Single-section doc (`section1` only).
  - Doc with article + sections + subsections (full hierarchy).
  - Doc with phantom signature block that should be collapsed.
  - Doc with subdocument verdict — verifies synthetic record created + children re-parented.
- [ ] `tests/test_canonicalizer_sot.py` — runs canonicalizer on 5 SOT docs end-to-end, compares output against human ledger:
  - **Clause-id alignment ≥95%**: of clauses in the human ledger, what fraction have a matching `clause_id` in our output?
  - **Body-text WRatio ≥80%**: for matched clauses, average rapidfuzz score on body text.
  - **Hierarchy alignment ≥98%**: for matched clauses, is the derived `parent_canonical_path` the same as the human ledger's parent?

### Don't

- Don't compute `structural_depth` from dd or lex parser output. Compute it from `len(canonical_path) - 1`. See `docs/DECISIONS.md` §1.
- Don't use the `kind` field to encode depth. They're decoupled — `(a)` can be subsection at depth 1 OR sub_subsection at depth 2 depending on path.
- Don't fail the canonicalizer on records that exceed depth 7. Mark them `parse_quality="excluded"` and keep them — exclusion is for downstream aggregation, not for parsing.

---

## Phase 4 — End-to-end pipeline + CLI

Wire the canonicalizer into a full-corpus pipeline driven by typer.

### Artifacts to produce

- [ ] `src/clause_extract/hf_io.py`:
  - `read_corpus(token: str) -> Iterator[CorpusRow]` — streams from `arthrod/new3_results_master22017_274.59mb` via polars lazy parquet.
  - `write_canonical(records: Iterable[DocumentRecord], local_path: Path) -> None`.
  - `push_canonical(local_path: Path, repo_id: str, token: str) -> str` — uploads to HF, returns commit URL.
- [ ] `src/clause_extract/parsers.py`:
  - `run_doc2dict(html: str) -> list[NodeDict]` using `agreement_config.make_agreement_config()`.
  - `run_lex(plain_text: str) -> list[SpanDict]` using `lexnlp_sections_regex.section_spans_re`.
  - `extract_html(raw_document_content: str) -> str | None` — the `<DOCUMENT>` envelope unwrap.
  - `extract_plain_text(html: str) -> str` — bs4-based, decode numerical entity refs.
  - Cache parses to `data/cache/<corpus_repo_short>/{idx}.json`.
- [ ] `src/clause_extract/pipeline.py`:
  - `canonicalize_corpus(corpus_iter, *, output_path, push_to_hub, repo_id)` orchestrates parse → detect_subdocs → canonicalize → write.
  - Logs progress to loguru with per-doc timings.
  - Handles `parse_status` outcomes: `image_only` docs get `parse_status="image_only"`, empty `clauses=[]`. Don't crash.
  - Deterministic ordering: sort output by `corpus_idx`.
- [ ] `src/clause_extract/cli.py` — typer subcommands:
  - `canonicalize --doc-idx N` — single doc, prints/writes canonical record.
  - `canonicalize --corpus full [--limit N] [--output PATH] [--push-to-hub]` — full run.
  - `detect-subdocs [--limit N] [--output PATH]` — runs detector only, useful for spot-checks.
  - `validate-sot` — runs validation gate 2 against the SOT.

### Tests

- [ ] `tests/test_parsers.py` — unit tests for HTML extraction, plain-text extraction, cache hit/miss behavior. Use small inline HTML fixtures.
- [ ] `tests/test_pipeline.py` — end-to-end test on a 3-doc fixture corpus (use `tests/fixtures/mini_corpus.parquet`, hand-built). Asserts the pipeline produces a valid `DocumentRecord` for each doc and writes deterministic output.
- [ ] `tests/test_cli.py` — typer's `CliRunner`, smoke-test each subcommand with `--help` and a minimal invocation.

### Acceptance

- [ ] `uv run clause-extract canonicalize --doc-idx 4` succeeds and prints a valid `DocumentRecord`.
- [ ] `uv run clause-extract canonicalize --corpus full --limit 50 --output data/runs/test_50.parquet` completes in <2 minutes on a single core.
- [ ] `uv run clause-extract canonicalize --corpus full --output data/runs/full.parquet` (no `--push-to-hub`) completes in <30 minutes.
- [ ] All `parse_status` values accounted for; zero unhandled exceptions across the 1066-doc run.

### Don't

- Don't push to HF in Phase 4. `--push-to-hub` exists but is not exercised until phase 5.

---

## Phase 5 — HF dataset push

Push the canonicalized output to `arthrod/clause-prob-canonical` (HF, private dataset repo).

### Artifacts to produce

- [ ] `scripts/push_canonical_to_hf.py` — wraps `hf_io.push_canonical` with a CLI confirmation prompt (user types repo name to confirm).
- [ ] Updated `README.md` with worked-example commands that produce the public dataset URL and a row-count match.

### Acceptance

- [ ] Dataset exists at `https://huggingface.co/datasets/arthrod/clause-prob-canonical` (private).
- [ ] `polars.scan_parquet("hf://datasets/arthrod/clause-prob-canonical/...")` returns rows with the schema from `docs/SCHEMA.md`.
- [ ] Row count equals input corpus row count (1066), or exclusion count is documented in the dataset card.
- [ ] HF dataset card YAML includes `license: agpl-3.0` and `tags: [parsing, sec-edgar, legal-nlp]`.

---

## Phase 6 — Validation runs against the gates

Final pass before review.

### Validation gates (re-run; all four must pass)

- [ ] **Gate 1 — reconstruction quality**: `uv run scripts/measure_reconstruction.py` shows mean word coverage ≥95% per parser, no doc below 80%, truncation off (`--no-truncate`).
- [ ] **Gate 2 — canonical-ID parser**: `uv run pytest tests/test_canonical_id_parser_sot.py -m sot` — 100% pass on 973 SOT clauses.
- [ ] **Gate 3 — subdoc detector**: `uv run pytest tests/test_subdoc_detector_100sample.py` — ≥80% precision AND ≥85% recall on the 100-doc hand-verified sample.
- [ ] **Gate 4 — end-to-end determinism**: `uv run clause-extract canonicalize --corpus full --output data/runs/final.parquet` produces a deterministic dataset; running it twice produces byte-identical output (sort by `corpus_idx`, normalize JSON whitespace, then diff).

### Documentation update

- [ ] If implementation diverged from any spec, update the relevant `docs/*.md` file in the same PR. State the rationale in the PR description.
- [ ] Add a `docs/IMPLEMENTATION_NOTES.md` capturing decisions made during phases 1–5 that weren't pre-specified — e.g. body-extraction patches you applied, caching strategy, phantom-parent pattern set, body-reconciliation thresholds.

### Acceptance

- [ ] All four validation gates pass.
- [ ] All test files pass without `-m sot` skips when `HF_TOKEN` is set.
- [ ] Coverage targets met:
  - ≥80% line / ≥70% branch baseline across `src/clause_extract/`.
  - ≥90% line / ≥85% branch on `canonical_id_parser.py` and `subdoc_detector.py`.
- [ ] One PR for review.

---

## Working notes for Claude Code

**Inline working notes are required.** Per Arthur's preferences, emit terse declarative findings as you work. Examples of useful working notes:

- "Phantom signature pattern: dd nodes whose header_text matches `^By:\s*$` AND whose parent has class=`predicted header` AND comes after node where `body_len > 5000`. Captured 38 such cases in idx=68."
- "Lex over-shreds inline enums: dd has `5` as one node, lex splits into 5, 5(a), 5(b), 5(c) as siblings. Folding (a)–(c) into 5's body recovers structure."
- "rapidfuzz body comparison thresholds: 95+ = match, 80–95 = soft match (record but flag), <80 = mismatch (record both for audit). 50-doc fuzzy run mean was 95.6 — supporting 80 as the boundary."

These survive context compaction. The chat history doesn't.

**When tests fail, suspect the test first.** Check fixtures, mock wiring, assertions, test data — before changing production code. Never reduce coverage to fix a failing test; rewrite the test instead.

**Mock at the transport layer.** For `huggingface_hub` and `datasets`, use in-memory parquet fixtures or `respx` to stub HTTP. Never patch `huggingface_hub` methods directly.

**Determinism is the contract.** Two runs on the same input must produce byte-identical output. If you see non-determinism, fix it before merging — sort dicts, sort lists, pin random seeds, fix dataclass equality. The pipeline has zero LLM components and zero non-determinic libraries; non-determinism is a bug in your code.

**Don't be a peasant; don't be condescending.** This is a real engineering project with real downstream consumers. Push back if a spec entry looks wrong — open a PR with the proposed change AND the evidence. Be calm, evidence-based, over-informed.

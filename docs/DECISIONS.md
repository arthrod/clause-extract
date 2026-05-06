# Design decisions

This is the locked decision log. Each entry has: **decision**, **alternatives considered**, **evidence**, **what would change our mind**.

If you're Claude Code and you find yourself disagreeing with one of these decisions during implementation, open a PR with the proposed change and reference the relevant section here. Do not silently diverge.

---

## §1 — Why ID-string is the canonical depth source, not parser depth

### Decision

`structural_depth` is computed from the clause ID string itself: `len(canonical_path) - 1`. So `5` → 0, `5(a)` → 1, `5(a)(v)` → 2. doc2dict's `depth` and lexnlp's `level`/`abs_level` are demoted to **audit-only signals** (`audit.dd_tree_depth`, `audit.lx_level`, `audit.lx_abs_level`). They never participate in computing the canonical depth.

### Alternatives considered

- **A. Use doc2dict's tree depth** (parent/child relationships from the nested dict). Plausible because doc2dict produces a real tree.
- **B. Use lexnlp's `level` (relative) or `abs_level` (absolute)**. Plausible because lexnlp's hierarchy parser is purpose-built for legal sections.
- **C. Combine both — agree-when-they-agree, fall back to ID string.** A blended approach.
- **D. Use the ID string** (chosen).

### Evidence

We measured how well each parser tracks ID-implied depth across the 101 documents in the corpus that have at least 2 distinct decimal levels (the testable subset).

| Pattern | doc2dict | lexnlp |
| --- | ---: | ---: |
| **perfect** — depth signal increases ~+1 per decimal level | 22 (22%) | 6 (6%) |
| **flat** — all decimal levels collapse to the same signal | 40 (40%) | 81 (81%) |
| **inverted** — deeper IDs at shallower depth | 14 (14%) | 13 (13%) |
| **messy** — non-monotonic | 25 (25%) | 0 (0%) |

doc2dict tracks correctly in ~22% of multi-level docs. Lexnlp in ~6%. Lexnlp's flat dominance is structural: once its numeric-fallback regex (`abs_level=7`) registers, all subsequent decimal IDs collapse to the same `level` regardless of decimal count.

**Both parsers track depth correctly in 3 of 101 multi-level docs (3%).** In 75% of multi-level docs, neither parser's depth signal is reliable for ID-implied depth. The agreement zone is too small to use as a rule.

The measurement script lived at `/home/claude/parse_compare_full/` during the design session; the categorization output is preserved in `tests/fixtures/depth_tracking_measurement.json` (TODO: port).

### What would change our mind

- A new parser whose depth signal correlates with ID-implied depth at >80% perfect-tracking would let us re-evaluate B above.
- A different document corpus (non-EX-10) where the ID convention itself is unreliable. Then ID-string-based depth becomes brittle and parser-depth might be the better signal.

---

## §2 — Subdocuments are materialized as records (Option 2 of A/B/C/D)

### Decision

When the post-processor encounters a clause whose `canonical_path[0]` is a `subdocument` segment, it materializes a synthetic clause record at the document root with `kind="subdocument"`, `is_synthetic=true`, and an explicit `subdocument_class`. Direct children point to the subdocument record as `parent_canonical_path`.

### Alternatives considered

- **A. Subdocuments are an invisible naming convention.** No synthetic records; bare-subdoc parents resolve to `null`. Loses the sibling-grouping relationship (192 SOT clauses share a subdocument prefix).
- **B. Materialize all subdocs as synthetic records.** (Chosen.) Keeps the tree explicit. Costs ~12 synthetic records across the SOT, ~130 expected at full corpus scale (out of ~50K clauses, 0.26% overhead).
- **C. Two-tier `parent_canonical_path` semantics** — some parents point to records, some point to scopes. Adds branching for every consumer.
- **D. Reform the SOT** to eliminate Category-2 prefixes (`def`, `cs`, `ac`, `notice`). Cleanest but expensive — would require re-extracting 5 documents.

### Why Option 2 was chosen

Per Arthur (verbatim): *"this is not a classification task"*. Materializing both Category 1 (named-in-source agreements like Power Purchase Agreement, Attachment I) and Category 2 (extractor-side scope labels like `def`, `cs`) as subdocument records preserves all relationships. The `subdocument_class` flag is metadata that lets downstream consumers filter; it doesn't gate the materialization.

This buys: zero schema commitment now, full queryability later, structural uniformity, audit trail.

It costs: anyone writing analytics has to know `subdocument_class` exists and decide whether they want to filter by it. For a research-phase project, that cost is acceptable.

### Evidence

11 distinct subdocument prefixes were observed in the SOT across 25 docs:

| Prefix | Docs | Clauses | Category |
| --- | ---: | ---: | --- |
| `cl` | 1 | 56 | 1 (Frontier Letter Agreement clauses) |
| `msw_concession` | 1 | 40 | 1 (named PPP concession agreement) |
| `ppa` | 1 | 36 | 1 (Power Purchase Agreement) |
| `deed_of_assignment` | 1 | 32 | 1 (Deed of Assignment) |
| `att1` | 1 | 29 | 1 (Attachment I named in source) |
| `la` | 1 | 19 | 1 (Letter Agreement body) |
| `ac` | 1 | 15 | 2 (extractor label "airport cost items") |
| `def` | 1 | 10 | 2 (extractor label "definitions block") |
| `cs` | 1 | 8 | 2 (extractor label "cover sheet") |
| `notice` | 1 | 5 | 1 (Notice of Stock Option Award form) |
| `summary` | 1 | 3 | 1 (Summary envelope headers) |

### What would change our mind

- If aggregation queries become onerous because of the `subdocument_class` filter, we'd revisit and pick Option D (reform SOT).
- If the synthetic record count exceeds 5% of total clauses at full corpus scale (vs. expected 0.3%), the storage overhead is non-negligible and Option A becomes attractive.

---

## §3 — Subdocument detector v1 is the materialization gate

### Decision

A document gets synthetic subdocument records materialized only when the **detector v1** trips. The detector is conservative: it materializes only when there's positive evidence of a multi-subdoc structure. False negatives (real subdocs missed) leave the clauses flat; false positives create harmless synthetic records flagged as `auto_detected`.

The detector's algorithm and validation are in `docs/DETECTOR.md`.

### Alternatives considered

- **A. Always materialize** — every dot-prefix in a clause_id triggers a synthetic record. High precision on the SOT (where prefixes are human-curated) but unreliable on the full corpus where prefixes don't exist (we'd never trip).
- **B. Use the detector v1.** (Chosen.)
- **C. Hand-curated whitelist of "real subdocument names".** Brittle. New documents bring new names.

### Evidence

Detector v1 against the hand-verified 100-doc sample:

| Metric | Result |
| --- | ---: |
| Docs flagged | 12/100 |
| True positives | 9 (10 if you count idx=100 which I labeled wrong on first inspection) |
| False positives | 3 (Hilton-style appendix templates and Frontier multi-attachment doc) |
| False negatives | 1 (Stock Issuance + Reg Rights at idx=357) |
| **Precision** | **75%** (83% if idx=100 is counted as TP) |
| **Recall** | **90%** |

### What would change our mind

- Recall < 80% on a fresh 100-doc sample → detector is too conservative, expand the strong-pattern keyword set.
- Precision < 60% on a fresh 100-doc sample → detector is too generous, tighten the late-position requirement.
- A documented case where false-positive synthetic records cause downstream aggregation errors.

---

## §4 — Reading B convention for ledger inclusion

### Decision

The canonical ledger **excludes** clauses lacking explicit identifiers from the structural ledger but **preserves them in `plain_text`**. This applies to:

- WHEREAS recitals (when not numbered/labeled)
- WITNESSETH topicals
- NOW THEREFORE preambles
- Trailing unidentified prose after the last identified clause in a section (folded into the body of the physically adjacent last identified clause; `audit.extraction_notes` records this)

Numbered/labeled recitals (`recital_A`, `whereas.1`) ARE included as records.

### Rationale

The clause-presence statistics need a stable denominator. WHEREAS recitals are universally present in agreements and don't carry meaningful structural ID; including them inflates the ledger and biases per-doc clause counts. The full text is preserved in `plain_text` for any consumer that needs it.

### Evidence

This convention was applied across all 25 SOT extractions and validated via the canonical-ID parser at 100% (see §1 for the parser validation).

### What would change our mind

- A downstream analysis that requires WHEREAS-clause counts as part of its statistics. We'd add a parallel ledger or a flag and stop considering them automatic exclusions.

---

## §5 — The four meanings of `class="section"`

### Decision

The schema records `kind` as a labeling concept and `structural_depth` as the integer. The **same regex match** for `class="section"` can land at different `structural_depth` values depending on its `canonical_path` ancestry. Consumers reason about "is this a top-level Section vs a sub-Section" via the path, not via `kind` alone.

### The four cases

`class="section"` collapses four semantically distinct things:

1. **Statutory citation in body prose** ("Section 13(a) of the Securities Act"). MUST NOT match. Handled by anchored regexes in `agreement_config.py`.
2. **Division of the filing/exhibit** when one EX-10 contains multiple agreements. `structural_depth` varies.
3. **Top-level division of an agreement that has no Articles** — Section 1 IS the top. `structural_depth = 0`.
4. **Sub-division of an Article** — Article 5, Section 5.1. `structural_depth = 1` (under Article).

Cases 3 and 4 produce identical regex matches but structurally different things. The label `class="section"` is correct in both; the **hierarchical position** differs.

### Implication for aggregation

Do NOT use `kind` alone to compute "did this agreement have a Section X". Use `canonical_path`. A clause living at `["section:5"]` (case 3) and a clause living at `["article:5", "section:5.1"]` (case 4) are different positions in the hierarchy.

### What would change our mind

- A reliable structural feature in the source document that always disambiguates cases 3 vs 4 at parse time. Then we could lift the disambiguation up and avoid relying on path traversal.

---

## §6 — AGPL stance for the analytical scope

### Decision

The clause-probability project is **batch analytical work, no distribution, no users querying the lexnlp software**. AGPL is unrestrictive in this scope because the outputs (statistics, derived datasets) are not the AGPL-licensed software.

The `lexnlp_sections_regex.py` vendored port is AGPLv3. This repository's license is AGPL-3.0-or-later for compatibility.

### The boundary that matters (for Cicero, not for clause-prob)

When this work eventually feeds Cicero's products:

- **Backend processes contracts via in-process lexnlp call, returns Cicero-branded analysis to user** → defensible private use; lexnlp is a library doing internal work.
- **API exposes recognizably lexnlp output to users** → AGPL §13 risk depending on output distinguishability.
- **Tauri desktop app bundles lexnlp into the distributed binary** → AGPL §5 distribution clearly triggers; source must be available.

### What would change our mind

- Cicero shipping a product that exposes lexnlp output directly to end users. We'd need to revisit §13 compliance.

---

## §7 — Parser comparison verdicts (doc2dict vs lexnlp)

### Decision

**doc2dict is the structural spine.** It produces nested HTML-aware section trees and types headers via regex (config C, the lexnlp-informed `mapping_dict`). lexnlp's regex section detector is **the cross-validation arm** — its output goes into `audit` fields, not into the canonical structure.

### Evidence (50-doc stratified sample)

- Status agreement: 50/50
- Exhibit-marker agreement: 78%
- Article count within ±50%: 94%
- Section count within ±50%: 74%
- Median lex-to-dd ratio: 3.11 (lex over-counts on internally-numbered docs because every numbered list item triggers a section break)
- Per-stratum keyword_article_section: only 20% section agreement (lex matches inline statutory references that doc2dict's anchored regexes correctly reject)

### Fuzzy content matching (37 section_agree docs, rapidfuzz WRatio)

| View | Precision | Recall | F1 |
| --- | ---: | ---: | ---: |
| A — keyword headers only | 77.3% | 90.3% | 83.3% |
| B — all structures | 34.6% | 13.4% | 19.3% |

View B's poor performance is because lex inline-enum splits create many tiny spans that don't align with dd's structural sections. View A is the actionable signal: when both parsers agree on a keyword-bearing header, they agree at ~95% mean WRatio, 59% exact match.

### What would change our mind

- A new parser that consistently outperforms both (e.g., a fine-tuned ML segmenter with deterministic seeds). We'd evaluate it as a third arm before promoting it.

---

## §8 — Determinism is reproducibility, not regex-vs-ML

### Decision

"Deterministic" in this project means **same input produces same output across runs**. It does NOT mean "regex over ML in the legal-defensibility sense" (which would matter if we were making clause-presence claims to clients, which we're not).

The pipeline is deterministic because:

- `doc2dict` parsing is regex + visual-cue heuristics, no model drift.
- `lexnlp_sections_regex` is a pure-regex port (the ML path is not used).
- The canonical-ID parser is regex-based.
- The subdocument detector is rule-based with hand-curated thresholds.

Determinism is enforced by pinning all dependency versions in `pyproject.toml`. The whole pipeline — parse, detect subdocuments, build canonical records, write dataset — is fully deterministic.

### What would change our mind

Nothing in this project's scope. This is a parsing pipeline; introducing model-based components would change its character.

---

## §9 — Image-only docs are excluded with a recorded count

### Decision

Documents whose HTML is just `<IMG SRC="...">` references with no visible text are detected by the parse stage (`parse_status="image_only"`) and excluded from the aggregate clause-presence statistics, with the exclusion count recorded.

No OCR is added to the pipeline.

### Evidence

In the 10-doc pilot, 2 of 10 (rows 178, 563) were image-only. Estimated corpus-wide rate is ~10-20%; needs measurement during full-corpus run.

### What would change our mind

- If image-only rate exceeds 30% at full corpus scale, the bias from exclusion may be too large. We'd add OCR upstream as a separate preprocessing step (not in this repo's scope).

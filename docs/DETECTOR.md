# Subdocument detector v1

The subdocument detector is the gate that decides whether a document gets synthetic `subdocument` records materialized. It runs on the doc2dict-parsed output for each document and produces a verdict: `no_subdoc | single_envelope | multi_subdoc_v1 | ambiguous`.

This document specifies the algorithm, its validation against a hand-verified 100-doc sample, and known failure patterns.

> **Pre-condition.** The detector reads doc2dict's output. If doc2dict isn't reconstructing the source faithfully (the immediate parsing goal — see [`GOAL.md`](GOAL.md)), the detector's input is unreliable and its precision/recall numbers won't generalize. This work is downstream of Phase 1 in [`../TASKS.md`](../TASKS.md). Detector tuning before parser quality is solved is wasted motion.

---

## What we're trying to detect

A **multi-subdocument document** is an EX-10 exhibit that bundles two or more logically distinct named documents under one filing wrapper. Examples:

- Credit Agreement + Form of Note + Form of Borrowing Request + Compliance Certificate (idx=68)
- Letter Agreement No. 8 + Attachments A-F (idx=243)
- Loan and Security Agreement + Notice of Borrowing + Multiple Certificates (idx=139)
- License Agreement + Form of Quality Agreement + Attachments A/B/C (idx=617)
- Stock Issuance Agreement + Registration Rights Agreement (idx=357)
- Letter Agreement to Aircraft Purchase Agreement + Replacement Clauses + Exhibits (idx=100)

A **single-document EX-10** is the common case: one main agreement, possibly with simple appendices that aren't subdocuments. Detector should not flag these.

The challenge: the agreement title typically appears in the TOC, the body header, and page rules — so naive pattern matching catches the same title 3-4 times and over-flags.

## Algorithm

```
def detect(dd_node_records: list[NodeRecord]) -> Verdict:
    candidates = scan_for_subdoc_patterns(dd_node_records)

    if not candidates:
        return Verdict("no_subdoc", n_synthesized=0)

    # 1. Distinct-string deduplication
    distinct = group_by(candidates, key=normalize_for_dedup)
    # Each distinct entry keeps its first occurrence's position

    # 2. Main-title filter: drop named_doctype matches in the first 5% of the doc
    after_main_filter = [
        d for d in distinct
        if not (d.pattern == "named_doctype"
                and d.first_position <= max(5, 0.05 * total_nodes))
    ]

    # 3. Strong-pattern shortcut
    has_strong = any(d.pattern in {"form_of", "attachment"} for d in distinct)

    # 4. Decision
    if len(after_main_filter) >= 2:
        return Verdict("multi_subdoc_v1", candidates=after_main_filter)
    if has_strong:
        # Single FORM OF / ATTACHMENT is also a real subdoc situation
        return Verdict("multi_subdoc_v1", candidates=distinct)
    if len(distinct) == 1 and distinct[0].pattern in {"appendix", "annex", "exhibit_named", "schedule_named"}:
        return Verdict("single_envelope", candidates=distinct)
    return Verdict("ambiguous", candidates=distinct)
```

### Normalization for dedup

```python
def normalize_for_dedup(header: str) -> str:
    h = header.lower()
    h = re.sub(r"\s+", " ", h)
    h = re.sub(r"[^\w\s]", "", h)
    return h.strip()
```

This collapses `"NOTICE OF AWARD"`, `"NOTICE OF\nAWARD"`, `"Notice of Award"` to the same key. Without this, the same title appearing in TOC + body + page-rule was triggering 3 distinct candidates.

### Patterns scanned

The scan_for_subdoc_patterns step matches against this ordered list of regexes (first match wins per node):

| Pattern label | Regex (case-insensitive unless noted) | Notes |
| --- | --- | --- |
| `form_of` | `^\s*FORM\s+OF\s+([A-Z][A-Z\s&'\-,]{2,80})\s*$` (case-sensitive) | Strong indicator. |
| `attachment` | `^\s*ATTACHMENT\s+([IVX0-9A-Z]+)\b.*` | Strong indicator. |
| `exhibit_named` | `^\s*EXHIBIT\s+([0-9A-Z\.\-]+)\s*[—:\-–]\s*.+` | "Exhibit X-1 — Description". |
| `schedule_named` | `^\s*SCHEDULE\s+([0-9A-Z\.\-]+)\s*[—:\-–]\s*.+` | |
| `appendix` | `^\s*APPENDIX\s+([0-9A-Z\.\-]+)\s*[—:\-–]?\s*.*` | |
| `annex` | `^\s*ANNEX\s+([0-9A-Z\.\-]+)\b.*` | |
| `notice_of` | `^\s*NOTICE\s+OF\s+([A-Z][A-Z\s&'\-,]{2,80})\s*$` (case-sensitive) | |
| `letter_agreement` | `^\s*(LETTER\s+AGREEMENT(?:\s+NO\.?\s*[\dA-Z]+)?)\s*$` | |
| `named_doctype` | `^\s*([A-Z][A-Z\s&'\-,]+(?:AGREEMENT\|NOTE\|GUARANTY\|RELEASE\|COVENANTS\|CERTIFICATE\|WAIVER))\s*$` (case-sensitive) | Most ambiguous; subject to main-title filter. |

Note: `form_of` and `notice_of` enforce ALL-CAPS via case-sensitivity, which is the convention for these section markers in formal SEC filings.

## Validation

### Ground truth

Hand-verified against a deterministic 100-doc sample (random seed=2025, drawn from the 1,006 successfully-parsed docs in the full 1066-doc corpus). The full sample is recorded in `tests/fixtures/subdoc_100sample_ground_truth.json` (TODO: port from session artifacts).

The 10 hand-verified **real multi-subdoc docs** in the sample:

| idx | Description |
| --- | --- |
| 68 | Credit Agreement + Form of Note + Form of Borrowing Request + Form of Interest Election Request + Form of Assignment + Compliance Cert + Solvency Cert |
| 100 | Frontier/Republic Airbus A320 Family Aircraft Purchase Agreement + Replacement Clause Appendices + Multiple Exhibits |
| 113 | Severance Waiver + Annex A (Severance Agreement being assigned) |
| 121 | Ninth Amendment to Credit Card Agreement + attached Subordination Agreement |
| 139 | Loan and Security Agreement + Multiple form attachments (Notes, Certificates, Notices) |
| 243 | Letter Agreement No. 8 + Attachments A-F |
| 357 | Stock Issuance Agreement + Registration Rights Agreement |
| 504 | Executive Employment Agreement + Addendum re Employment Agreement |
| 617 | Zydis Development and License Agreement + Attachments A/B/C + Form of Quality Agreement |
| 840 | Share Option Grant Notice + Option Agreement + Attachments I/II/III + Notice of Exercise |
| 1054 | Registration Rights Agreement + Form of Assignment and Joinder |

### Detector v1 results

| Metric | Value |
| --- | ---: |
| Docs flagged by v1 | 12 |
| True positives | 9 |
| False positives | 3 |
| False negatives | 1 |
| **Precision** | **75%** |
| **Recall** | **90%** |

If we re-classify idx=100 as a true positive (which it is — I missed it in my first hand-verification pass), the numbers become **83% precision / 91% recall**.

### Known false positives

| idx | Why detector tripped | Why it's not a real multi-subdoc |
| --- | --- | --- |
| 222 | Hilton FORM OF RSU Agreement template with Appendix A/B/C (likely country-specific terms or vesting schedules) | The appendices look like exhibits to one logical document, not separate named subdocuments. **Borderline.** |
| 239 | Same pattern as 222 but for Stock Options | Same. **Borderline.** |
| (idx=100) | (Was a false positive in my first pass; correctly a TP on second look) | — |

Both 222 and 239 are arguably ambiguous. The detector's behavior is acceptable here because the synthetic records it would create are flagged `subdocument_class="auto_detected"` and consumers can filter.

### Known false negative

| idx | Why detector missed | Pattern |
| --- | --- | --- |
| 357 | Only 2 distinct candidates (`STOCK ISSUANCE AGREEMENT` filtered as main title; `REGISTRATION RIGHTS AGREEMENT` left as 1 distinct after filter). Doesn't trip `>=2` rule and `REGISTRATION RIGHTS` isn't a strong pattern. | `[main_title_filtered] [single late-position named_doctype]` |

This is a real gap. To close it without sacrificing precision, we tested a relaxation: "1 distinct after main filter AND it's past the 50% position mark counts." That relaxation pulled in 9 new false positives (precision dropped to 52%) — too costly.

A more targeted fix would be to add `REGISTRATION RIGHTS`, `SUBORDINATION`, `JOINDER` to the strong-pattern set as recognized "second-agreement-attachment" keywords. This is a planned v2 improvement but NOT part of v1.

## Generalization to the full corpus

The v1 detector has only been validated against the 100-doc sample. Before relying on it for the full 1066-doc canonicalization run, **Phase 1 of the implementation must run the detector across the full corpus and hand-spot-check 5-10 random "flagged multi-subdoc" docs**. If precision holds at ≥75% on a fresh sample, proceed. If it drops below, fix before Phase 2.

Expected scale at full corpus: ~10-12% of docs flagged as multi-subdoc, so ~110-130 docs. Synthetic-record overhead: ~130-260 records (each flagged doc gets 1-2 synthetic subdoc records on average).

## Detector v2 candidates (deferred)

Improvements being considered for after v1 validates at full scale:

1. **Strong-pattern keyword expansion** — add `REGISTRATION RIGHTS`, `SUBORDINATION`, `JOINDER`, `WARRANT`, `PROMISSORY NOTE`, `CONSENT` to the strong-pattern set. Closes the idx=357 false-negative class.
2. **Position-aware named_doctype** — a `named_doctype` match in the LAST 25% of the doc with no main-title competition (i.e., the only candidate of its type) should also count. Closes some FN cases where the main title isn't matched at all.
3. **Cross-document prefix consistency** — if the same prefix (e.g., `ppa.`) appears 3+ times in the same document, that's evidence of a real namespace. Useful when prefix matches don't trip pattern-based rules.
4. **Heuristic per `subdocument_class`** — distinguish category_1 (named in source) from category_2 (extractor labels) at detection time using whether the prefix string appears verbatim in `plain_text`.

These are NOT in scope for v1.

## Implementation notes

- The detector is a pure function: `detect(dd_nodes: list[Node]) -> Verdict`. No I/O, no side effects, no external state.
- Property-based tests (hypothesis) are appropriate for the normalize_for_dedup function.
- The 100-doc sample test should run `detect` against the cached parses and assert ≥80% precision and ≥85% recall (the validation gate). Results below that are treated as failing tests.

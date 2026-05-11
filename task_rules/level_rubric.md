# Level rubric — nesting depth 0 through 7

## The parser's only goal

Slice each agreement's HTML into clauses with hierarchy — clause **text**
plus structural **nesting depth** — so that concatenating the slices in
document order reconstructs the source document faithfully.

That single criterion drives parser quality. Levels exist to capture
nesting depth; depths exist to make hierarchy explicit. Everything else
(canonical schema, subdocument detection, classification, statistics) is
downstream. If concat-of-spans doesn't reproduce the source for an idx,
the parser is wrong for that idx — regardless of how clever its level
assignments look.

## Scope: every kind of agreement is in scope

The corpus is SEC EX-10 attachments, but the agreements themselves come
in many shapes. **Document type does not matter.** All of these are
in-scope and should be parsed by the same rubric:

  - Private bilateral contracts (credit agreements, license deals).
  - Public / regulated agreements (government contracts and
    amendments, e.g. SF-30; tariff schedules).
  - International / cross-border contracts.
  - **Unilateral agreements**: guaranties, options, releases,
    designations of agent, irrevocable proxies — where only one
    party makes a binding promise.
  - Multilateral and complex tri-party agreements.

The structural patterns the parser detects (numbered Sections,
lettered subsections, signature lines, attached subdocuments) are
common across all of these. Don't add document-class-specific code
paths; the structural patterns themselves are document-class neutral.

What's *not* in scope is **non-agreement metadata** — see
[`scope_rule.md`](scope_rule.md). That's filing-context content
surrounding the agreement (the SEC `<DOCUMENT>` envelope, post-
signature filing trailers like press releases or company
boilerplate). Those get tagged `scope="trailer"` and stay out of
the JSONL; they do not affect which agreement types we accept.

## JSONL record shape

The parser at `scripts/parse_doc2dict_with_config.py` emits one JSONL
record per parsed unit. Each record has exactly three keys:

```json
{"idx": <int>, "level": <int>, "span": "<heading>\n<body>"}
```

  - `idx` — corpus row index.
  - `level` — **nesting depth** (0-indexed) of this clause within the
    agreement's structural hierarchy. The mapping below is the contract.
  - `span` — heading + body verbatim. Concatenating all `span` values
    for one `idx` in JSONL line order should reconstruct the source
    document's clause text.

## Nesting depths for a standalone agreement (no subdocuments)

| depth | what it represents | examples |
|-------|--------------------|----------|
| 0     | the agreement itself: title line + preamble | "INDEMNIFICATION AGREEMENT\nTHIS INDEMNIFICATION AGREEMENT (the 'Agreement') is made…" |
| 1     | top-level headings and recital blocks: party / exhibit metadata, WHEREAS recitals, signature block | "ULURU Inc.", "WITNESSETH THAT:\nWHEREAS…", "INDEMNITEE", "/s/ Vaidehi Shah" |
| 2     | numbered Sections | "1. Indemnity of Indemnitee", "2. Additional Indemnity", "Section 6", "10." |
| 3     | lettered subsections | "(a) Proceedings…", "(b)…", "(c)…" |
| 4     | sub-sub items | "(i)", "(ii)", "(A)", "(B)", "(1)", "(2)" |
| 5     | deeper roman/letter nesting inside 4-depth items | rare in EX-10s but possible |
| 6     | depth 5 inside a subdocument (penalty +1) — see below | |
| 7     | depth 6 inside a subdocument | |

For documents with a flatter structure (a form, a one-page release, a
unilateral designation), the deeper depths may simply not appear —
that's correct, not a defect. The depths capture the structure that
exists in the source. Don't force depth that isn't there.

## SEC envelope drop rule

SEC EDGAR filings wrap each EX-10 contract in a `<DOCUMENT>` envelope
whose first visible line is the exhibit number, e.g.:

```
EXHIBIT 10.25
ULURU Inc.
INDEMNIFICATION AGREEMENT
THIS INDEMNIFICATION AGREEMENT (the "Agreement") is made…
```

The literal "EXHIBIT 10.25" line is filing metadata — non-agreement
content. The parser drops it from the JSONL (it is kept in the parquet
via the `is_envelope=true` flag, so audit tooling can recover it).

Mechanically: the FIRST section per idx whose `cls` is one of
`exhibit / schedule / appendix / annex` is treated as the envelope. Its
descendants — i.e. the actual agreement — are NOT subject to the
subdocument penalty (see below).

## Subdocument depth penalty

A real subdocument is an attached document inside the main agreement:
"EXHIBIT A — FORM OF NOTICE", "SCHEDULE 2 — DEFINITIONS", an "ANNEX I"
addendum, etc. These appear AFTER the main agreement body, not in the
SEC-envelope position.

Each level of subdocument nesting adds **+1** to the depth of every
descendant. The subdocument header itself sits at depth 1 of the
enclosing document (or higher if itself nested).

Example: if a section pattern matches "(a)" inside a top-level
subdocument, its inferred depth is 3 (lettered subsection) and its
emitted depth is `3 + 1 = 4`. If the same "(a)" appears inside an
exhibit-inside-an-exhibit, the emitted depth is `3 + 2 = 5`.

The rule extends the rubric naturally up to depth 7:

| context                                  | (a) becomes | (i) becomes |
|------------------------------------------|-------------|-------------|
| main agreement                           | 3           | 4           |
| inside one subdocument                   | 4           | 5           |
| inside subdoc-inside-subdoc              | 5           | 6           |
| inside subdoc-inside-subdoc-inside-subdoc | 6           | 7           |

Three levels of subdoc nesting is the practical ceiling — most filings
top out at one level (a single attached exhibit). The rubric supports
deeper.

## What "matches the rubric" means in practice

Run the parser, look at the JSONL for the current idx, and check:

1. **Exactly ONE depth-0 record per idx** — the agreement's title +
   preamble. Multiple depth-0s indicate the parser misread the
   document structure (split into multiple roots, or captured a
   filing trailer as a second "agreement").
2. **Max depth ≤ 7.** Anything higher means the parser captured noise
   (post-signature trailer boilerplate, page-footer artifacts).
3. **The first record is not the SEC envelope marker.** "EXHIBIT
   10.25" with no body should be dropped (envelope detection).
4. **No filing-trailer boilerplate captured.** See
   [`scope_rule.md`](scope_rule.md) for what "filing trailer" means
   and how it's filtered structurally.

The depth distribution is whatever the document's structure produces.
A flat unilateral designation will have mostly depth-1 records and no
depth-2/3/4 records — that is correct.

## Reconstruction faithfulness — the underlying bar

The rubric checks above (1 L0, max ≤ 7, etc.) are necessary but not
sufficient. The real test is reconstruction: take all spans for one
idx in JSONL line order, concatenate them, and compare to
`parse_source_of_truth.jsonl[idx].span_clean` (the bs4 plain-text
extraction of the source HTML).

Two measurements (computed by
[`scripts/measure_reconstruction.py`](../scripts/measure_reconstruction.py)
and surfaced as a non-blocking warning by `freeze.py`):

  - **word coverage**: |source_words ∩ reconstructed_words| /
    |source_words| × 100. 100% means every unique word in the source
    appears at least once in the reconstruction.
  - **char ratio**: len(reconstructed) / len(source) × 100. Around
    100% means the reconstruction has the same character volume; far
    below means content was dropped; far above means content was
    duplicated.

Project-level bar (from
[`../docs/DECISIONS.md`](../docs/DECISIONS.md) §10): mean word
coverage ≥ 95% across the corpus, no doc below 80%. Per-idx
reconstruction below the bar is a warning, not a blocker — the freeze
still goes through, but the warning surfaces in the freeze output and
in the next dispatch's attempt-history banner so the agent can
investigate before moving on.

## See also

- [`examples_main_agreement.md`](examples_main_agreement.md) — fully
  worked example for a typical Indemnification Agreement.
- [`examples_with_subdocs.md`](examples_with_subdocs.md) — fully
  worked example showing the +1 penalty in action.
- [`scope_rule.md`](scope_rule.md) — agreement-vs-trailer
  classification using the structural scope rule.
- [`freeze_command.md`](freeze_command.md) — what the freeze
  validator checks and what the non-blocking reconstruction warning
  tells you.

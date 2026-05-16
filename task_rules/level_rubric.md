# Level rubric — nesting depth 0 through 7

## The parser's only goal

Slice each agreement's HTML into clauses with hierarchy — clause **text**
plus structural **nesting depth** — so that concatenating the slices in
document order reconstructs the source document faithfully.

That single criterion drives parser quality. The `level` field is the
clause's 0-indexed nesting depth. If concat-of-spans doesn't reproduce
the source for an idx, the parser is wrong for that idx — regardless of
how clever its depth assignments look.

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

What's *not* in scope is **non-agreement metadata** — see
[`scope_rule.md`](scope_rule.md). That's filing-context content
surrounding the agreement (the SEC `<DOCUMENT>` envelope, post-
signature filing trailers like press releases or company boilerplate).
Those get tagged `scope="trailer"` and stay out of the JSONL; they do
not affect which agreement types we accept.

## JSONL record shape

The parser at `scripts/parse_doc2dict_with_config.py` emits one JSONL
record per parsed unit. Each record has exactly four keys:

```json
{"idx": <int>, "order": <int>, "level": <int>, "span": "<heading>\n<body>"}
```

  - `idx` — corpus row index.
  - `order` — 0-indexed sequence number *within* this idx, in document
    order. The first emitted record for idx=N has `order=0`, the next
    has `order=1`, and so on. JSON dicts are not ordered; `order`
    makes the document sequence explicit so downstream consumers can
    reconstruct it regardless of how the JSONL was loaded.
  - `level` — **nesting depth** (0-indexed) of this clause within the
    agreement's structural hierarchy. The mapping below is the
    contract.
  - `span` — heading + body verbatim. Concatenating all `span` values
    for one `idx` in `order` ascending should reconstruct the source
    document's clause text.

## Nesting depths for a standalone agreement (no subdocuments)

| depth | what it represents | examples |
|-------|--------------------|----------|
| 0     | the agreement **title** alone, exactly ONE record per idx | "INDEMNIFICATION AGREEMENT", "CREDIT AGREEMENT", "DESIGNATION OF AGENT AGREEMENT" |
| 1     | every direct child of the agreement — preamble paragraph, recital block, top-level body clauses, signature block | "THIS INDEMNIFICATION AGREEMENT (the 'Agreement') is made…", "WITNESSETH THAT: WHEREAS…", "1. Indemnity of Indemnitee", "ARTICLE I — DEFINITIONS", "/s/ Vaidehi Shah" |
| 2     | direct child of an L1 clause — Sections under an Article, or lettered subsections under a top Section | "Section 1.1 Definitions" (under ARTICLE I), "(a) Proceedings Other Than…" (under "1. Indemnity") |
| 3     | direct child of an L2 clause — lettered under Sections-under-Articles, or roman/letter/digit items under (a) | "(a) Definitions" (under Section 1.1), "(i) by a majority vote…" (under "(a) Proceedings") |
| 4     | direct child of an L3 clause | rare; deeper roman/letter/digit nesting |
| 5     | direct child of an L4 clause | very rare |
| 6     | depth 5 inside a subdocument (penalty +1) — see below | |
| 7     | depth 6 inside a subdocument | |

The **first top-level body clause** sits at depth 1 — whether that
clause is an Article (in agreements that use Article/Section nesting)
or a Section (in agreements that go straight to numbered Sections). Its
children are at depth 2. The depth is determined by structural
ancestry, not by what the clause is *called*.

For documents with a flatter structure (a form, a one-page release, a
unilateral designation), the deeper depths may simply not appear —
that's correct, not a defect. The depths capture structure that exists
in the source. Don't synthesise depth that isn't there.

## SEC envelope drop rule

SEC EDGAR filings wrap each EX-10 agreement in a `<DOCUMENT>` envelope
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

Example: a top-level Section inside a subdocument starts as depth 1
under its enclosing subdoc structure; the +1 subdoc penalty bumps it
to depth 2. A lettered `(a)` subsection inside that Section is depth
2 → 3. A `(i)` further inside is depth 3 → 4. For a subdoc inside a
subdoc, add +2; for three levels of subdoc nesting, +3 (ceiling 7).

| context                                     | first body clause | (a) becomes | (i) becomes |
|---------------------------------------------|-------------------|-------------|-------------|
| main agreement                              | 1                 | 2           | 3           |
| inside one subdocument                      | 2                 | 3           | 4           |
| inside subdoc-inside-subdoc                 | 3                 | 4           | 5           |
| inside subdoc-inside-subdoc-inside-subdoc   | 4                 | 5           | 6           |

Three levels of subdoc nesting is the practical ceiling — most filings
top out at one level. The rubric supports deeper through depth 7.

## What "matches the rubric" means in practice

Run the parser, look at the JSONL for the current idx, and check:

1. **Exactly ONE depth-0 record per idx** — the agreement's title
   alone. Multiple depth-0s indicate the parser misread the document
   structure (split into multiple roots, or captured a filing trailer
   as a second "agreement"). Zero depth-0s means the parser failed to
   identify the title.
2. **Max depth ≤ 7.** Anything higher means the parser captured noise
   (post-signature trailer boilerplate, page-footer artifacts).
3. **The first record is not the SEC envelope marker.** "EXHIBIT
   10.25" with no body should be dropped (envelope detection).
4. **`order` is monotonic** within each idx (0, 1, 2, …) and matches
   document order — first emitted span is `order=0`.
5. **No filing-trailer boilerplate captured.** See
   [`scope_rule.md`](scope_rule.md) for what "filing trailer" means
   and how it's filtered structurally.

The depth distribution is whatever the document's structure produces.
A flat unilateral designation will have one depth-0, several depth-1
records, and maybe nothing deeper — that is correct.

## Reconstruction faithfulness — the blocking gate

The rubric checks above are necessary but not sufficient. The real test
is reconstruction: take all spans for one idx ordered by `order`,
concatenate them, and compare to
`parse_source_of_truth.jsonl[idx].span_clean` (the bs4 plain-text
extraction of the source HTML).

Two measurements (computed by
[`scripts/measure_reconstruction.py`](../scripts/measure_reconstruction.py)
and enforced by `freeze.py`):

  - **word coverage**: |source_words ∩ reconstructed_words| /
    |source_words| × 100. 100% means every unique word in the source
    appears at least once in the reconstruction.
  - **char ratio**: len(reconstructed) / len(source) × 100. Around
    100% means the reconstruction has the same character volume; far
    below means content was dropped; far above means content was
    duplicated.

Project bar (from [`../docs/DECISIONS.md`](../docs/DECISIONS.md) §10):
**word coverage ≥ 90% per idx.** `freeze.py` enforces this as a
**blocking gate** — a per-idx reconstruction below 90% refuses the
freeze and prints the missing-word sample so the agent can localize
the gap. The bar is per-idx, not just per-corpus, so individual
baselines can't sneak in below the line.

## See also

- [`examples_main_agreement.md`](examples_main_agreement.md) — fully
  worked example for a typical Indemnification Agreement (no subdocs).
- [`examples_with_subdocs.md`](examples_with_subdocs.md) — fully
  worked example showing the +1 subdocument penalty in action.
- [`scope_rule.md`](scope_rule.md) — agreement-vs-trailer
  classification using the structural scope rule.
- [`freeze_command.md`](freeze_command.md) — what the freeze
  validator checks and the 90% reconstruction gate.

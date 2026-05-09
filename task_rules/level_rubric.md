# Level rubric — 0 through 7

The parser at `scripts/parse_doc2dict_with_config.py` emits one JSONL
record per parsed unit. Each record has exactly three keys:

```json
{"idx": <int>, "level": <int>, "span": "<heading>\n<body>"}
```

`level` is a 0-indexed depth into the agreement's structural hierarchy.
The mapping below is the contract.

## Levels for a STANDALONE agreement (no subdocuments)

| level | what it represents | examples |
|-------|--------------------|----------|
| 0     | the agreement itself: the title line + preamble | "INDEMNIFICATION AGREEMENT\nTHIS INDEMNIFICATION AGREEMENT (the 'Agreement') is made…" |
| 1     | top-level headings and recital blocks: party / exhibit metadata, WHEREAS recitals, signature block | "ULURU Inc.", "WITNESSETH THAT:\nWHEREAS…", "INDEMNITEE", "/s/ Vaidehi Shah" |
| 2     | numbered Sections | "1. Indemnity of Indemnitee", "2. Additional Indemnity", "Section 6", "10." |
| 3     | lettered subsections | "(a) Proceedings…", "(b)…", "(c)…" |
| 4     | sub-sub items | "(i)", "(ii)", "(A)", "(B)", "(1)", "(2)" |
| 5     | sub-sub-sub items: deeper roman/letter nesting inside 4-level items | rare in EX-10s but possible |
| 6     | level 5 inside a subdocument (penalty +1) — see below | |
| 7     | level 6 inside a subdocument | |

## SEC envelope drop rule

SEC EDGAR filings wrap each EX-10 contract in a `<DOCUMENT>` envelope
whose first visible line is the exhibit number, e.g.:

```
EXHIBIT 10.25
ULURU Inc.
INDEMNIFICATION AGREEMENT
THIS INDEMNIFICATION AGREEMENT (the "Agreement") is made…
```

The literal "EXHIBIT 10.25" line is filing metadata, NOT contract
content. The parser drops it from the JSONL output (it is still kept in
the parquet via the `is_envelope=true` flag, so downstream tooling can
recover it).

Mechanically: the FIRST section per idx whose `cls` is one of
`exhibit / schedule / appendix / annex` is treated as the envelope. Its
descendants — i.e. the actual contract — are NOT subject to the
subdocument penalty (see below).

## Subdocument level penalty

A real subdocument is an attached document inside the main contract:
"EXHIBIT A — FORM OF NOTICE", "SCHEDULE 2 — DEFINITIONS", an "ANNEX I"
addendum, etc. These appear AFTER the main contract body, not in the
SEC-envelope position.

Each level of subdocument nesting adds **+1** to the level of every
descendant. The subdocument header itself sits at level 1 of the
enclosing document (or higher if itself nested).

Example: if a section pattern matches "(a)" inside a top-level
subdocument, its inferred level is 3 (lettered subsection) and its
emitted level is `3 + 1 = 4`. If the same "(a)" appears inside an
exhibit-inside-an-exhibit, the emitted level is `3 + 2 = 5`.

The rule extends the rubric naturally up to level 7:

| context                                  | (a) becomes | (i) becomes |
|------------------------------------------|-------------|-------------|
| main agreement                           | 3           | 4           |
| inside one subdocument                   | 4           | 5           |
| inside subdoc-inside-subdoc              | 5           | 6           |
| inside subdoc-inside-subdoc-inside-subdoc | 6           | 7           |

Three levels of subdoc nesting is the practical ceiling — most filings
top out at one level (a single attached exhibit). The rubric supports
deeper.

## What "match the rubric" means in practice

Run the parser, look at the JSONL for the current idx, and check three
things:

1. **There is exactly ONE level-0 record per idx**, and it's the
   agreement's title + preamble. Multiple level-0s indicate the parser
   misread the document structure.
2. **The level distribution is monotonically non-increasing in count**
   for typical agreements (more L3 records than L2, more L2 than L1).
   Exceptions are rare and worth noting.
3. **No "EXHIBIT <number>" envelope marker** appears in the JSONL —
   that should be dropped. If it shows up, the envelope detection
   failed.

## See also

- [`examples_main_agreement.md`](examples_main_agreement.md) — fully
  worked example for a typical Indemnification Agreement.
- [`examples_with_subdocs.md`](examples_with_subdocs.md) — fully
  worked example showing the +1 penalty in action.

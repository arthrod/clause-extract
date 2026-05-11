# Worked example — agreement with attached subdocuments (depths 0–7)

This example shows how the +1 subdocument penalty propagates. It is
SYNTHETIC — built to exercise every nesting depth from 0 through 7
(the `level` field in JSONL records). The actual SEC corpus rarely
goes deeper than depth 5, but the parser supports the full range.

## Source structure (synthetic agreement with nested subdocs)

```
[SEC envelope, dropped from JSONL]
EXHIBIT 10.99

[main contract]
ACME CO.
SHAREHOLDERS AGREEMENT
THIS SHAREHOLDERS AGREEMENT…

WITNESSETH THAT:
WHEREAS, …

1. Definitions
  (a) "Affiliate" means…
    (i) any direct or indirect owner of more than 10%…

2. Voting Rights
  (a) Each Share shall be entitled to one vote.

[signature block]
ACME CO.
By: /s/ Jane Doe

[FIRST subdocument — adds +1 penalty to its descendants]
EXHIBIT A — FORM OF JOINDER
THIS JOINDER AGREEMENT is entered into…

WHEREAS, the undersigned wishes to become a party…

1. Joinder
  (a) The undersigned hereby joins the Shareholders Agreement.
    (i) Effective on the date of this Joinder.

[NESTED subdocument — adds another +1 penalty]
SCHEDULE 1 TO EXHIBIT A — TRANSFER TERMS

THIS SCHEDULE OF TRANSFER TERMS sets out…

1. Permitted Transferees
  (a) An Affiliate of the holder.
    (i) Any direct subsidiary.

[DOUBLY-NESTED subdocument — third +1 penalty]
ANNEX I TO SCHEDULE 1 — DEFINITIONS

THIS ANNEX defines the terms used in the Schedule.

1. Listed Terms
  (a) "Subsidiary" means an entity controlled by the holder.
    (i) Direct ownership of more than 50% of voting power.
```

## Expected JSONL output (excerpt, levels annotated)

```jsonl
[main contract — no penalty]
{"level":1, "span":"ACME CO."}
{"level":0, "span":"SHAREHOLDERS AGREEMENT\nTHIS SHAREHOLDERS AGREEMENT…"}
{"level":1, "span":"WITNESSETH THAT:\nWHEREAS, …"}
{"level":2, "span":"1. Definitions"}
{"level":3, "span":"(a) 'Affiliate' means…"}
{"level":4, "span":"(i) any direct or indirect owner of more than 10%…"}
{"level":2, "span":"2. Voting Rights"}
{"level":3, "span":"(a) Each Share shall be entitled to one vote."}
{"level":1, "span":"ACME CO.\nBy: /s/ Jane Doe"}

[Exhibit A — first subdoc, penalty=1 applies to its descendants]
{"level":1, "span":"EXHIBIT A — FORM OF JOINDER\nTHIS JOINDER AGREEMENT is entered into…"}
{"level":2, "span":"WHEREAS, the undersigned wishes to become a party…"}    # was 1, +1
{"level":3, "span":"1. Joinder"}                                              # was 2, +1
{"level":4, "span":"(a) The undersigned hereby joins…"}                       # was 3, +1
{"level":5, "span":"(i) Effective on the date of this Joinder."}              # was 4, +1

[Schedule 1 — nested in Exhibit A, penalty=2]
{"level":2, "span":"SCHEDULE 1 TO EXHIBIT A — TRANSFER TERMS\nTHIS SCHEDULE…"} # was 1, +1
{"level":3, "span":"1. Permitted Transferees"}                                 # was 2, +1 (the Schedule's +1 applies to ITS internals, but Schedule itself is at +1 from Exhibit A's penalty)
                                                                                # i.e., 2 (numbered) + 1 (Exhibit) + 0 (just Schedule's title row, not its descendants) = 3
                                                                                # WAIT — let me re-read the rule below
```

## A precise statement of the penalty rule

(Easier to state as code than English.)

For each section, the parser computes:
- `remapped` — the inferred standalone level (0 for the root agreement,
  1 for the title, 2 for "1.", 3 for "(a)", 4 for "(i)", etc.).
- `subdoc_penalty` — the count of REAL subdocument ancestors (subdoc-
  class containers ABOVE this section, EXCLUDING the SEC envelope). It
  starts at 0 at the document root.
- `effective_level = remapped + subdoc_penalty`.

When recursing into a section's children, `child_penalty` is:
- `subdoc_penalty + 1` if the section is a real subdocument (subdoc
  class AND not the SEC envelope).
- `subdoc_penalty` otherwise.

So the SUBDOC HEADER itself sits at `1 + parent_penalty` (because EXHIBIT/
SCHEDULE/ANNEX patterns map to remapped=1). Its DIRECT descendants get
`parent_penalty + 1` applied.

## Worked level table for the synthetic example

| section                                      | standalone level | ancestor subdocs | emitted level |
|----------------------------------------------|------------------|------------------|---------------|
| SHAREHOLDERS AGREEMENT (main title)          | 0                | 0                | 0             |
| ACME CO. (party block)                       | 1                | 0                | 1             |
| WHEREAS recital                              | 1                | 0                | 1             |
| "1. Definitions"                             | 2                | 0                | 2             |
| "(a) 'Affiliate' means…"                     | 3                | 0                | 3             |
| "(i) any direct or indirect owner…"          | 4                | 0                | 4             |
| EXHIBIT A — FORM OF JOINDER (subdoc header)  | 1                | 0                | 1             |
| WHEREAS inside Exhibit A                     | 1                | 1 (Exhibit A)    | 2             |
| "1. Joinder" inside Exhibit A                | 2                | 1                | 3             |
| "(a)" inside Exhibit A                       | 3                | 1                | 4             |
| "(i)" inside Exhibit A                       | 4                | 1                | 5             |
| SCHEDULE 1 (subdoc header inside Exhibit A)  | 1                | 1 (Exhibit A)    | 2             |
| "1." inside Schedule 1                       | 2                | 2                | 4             |
| "(a)" inside Schedule 1                      | 3                | 2                | 5             |
| "(i)" inside Schedule 1                      | 4                | 2                | 6             |
| ANNEX I (subdoc header inside Schedule 1)    | 1                | 2                | 3             |
| "1." inside Annex I                          | 2                | 3                | 5             |
| "(a)" inside Annex I                         | 3                | 3                | 6             |
| "(i)" inside Annex I                         | 4                | 3                | 7             |

So levels 5, 6, and 7 only appear when content is nested 1, 2, or 3
subdocuments deep. The parser supports the full range; whether a
particular SEC filing actually exercises it depends on the doc.

## Edge cases

- A subdocument header SECTION whose body is empty AND that has children
  is still treated as a real subdocument; its descendants get +1.
- The SEC envelope (FIRST exhibit-class section per idx) does NOT count.
  Its descendants are "the main agreement" and get penalty=0.
- A section whose `cls` is something else (`predicted header`,
  `promoted text leaf`, `introduction`) never adds penalty.

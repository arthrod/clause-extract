# Worked example — agreement with attached subdocuments (depths 0–7)

This example shows how the +1 subdocument penalty propagates. It is
SYNTHETIC — built to exercise every nesting depth from 0 through 7
(the `level` field in JSONL records). The actual SEC corpus rarely
goes deeper than depth 5, but the parser supports the full range up
to the ceiling enforced by `freeze.py`.

The example uses the same structural rules as the main-agreement
example: title alone at depth 0, every direct child of the agreement
at depth 1, their children at depth 2, etc. The subdocument penalty
adds **+1 to every descendant** of a real subdocument (cls in
`{exhibit, schedule, appendix, annex}`, `is_envelope=False`, with
descriptive title text — see [`scope_rule.md`](scope_rule.md) for
the structural detection contract).

## Source structure (synthetic agreement with nested subdocs)

```
[main agreement body]
MASTER AGREEMENT
THIS MASTER AGREEMENT… (preamble)
RECITALS
1. Term and Termination
  (a) Initial Term…
  (b) Renewal…
2. Definitions
3. Confidentiality

[1st-level subdoc]
EXHIBIT A — FORM OF NOTICE
1. Scope of Notice
  (a) Required Recipients
    (i) by certified mail…
      (A) addressed to "Chief Officer"
        (1) with delivery confirmation

[1st-level subdoc, second one — header only, no body]
SCHEDULE 2 — DEFINITIONS

[2nd-level subdoc — Exhibit B contains Annex I]
EXHIBIT B — INTERIM PROCEDURES
1. Procedures
ANNEX I — WIRE INSTRUCTIONS
  (a) Bank Details
    (i) Routing
      (A) ACH protocol
        (1) initiation cutoff

[3rd-level subdoc — deepest example]
EXHIBIT C — FORM OF AMENDMENT
APPENDIX C-1 — TEMPLATE
SCHEDULE C-1-α — SETTLEMENT MATRIX
  (a) Tier Calculation
    (i) Annual Volume
      (A) Threshold
```

## How depths are assigned

For each node, the parser computes:

```
depth = (natural structural depth from the title down)
      + (number of enclosing real subdocuments at that node)
```

There are two cases worth stating explicitly because the rule is
asymmetric:

  - A subdocument **header** (the "EXHIBIT A — FORM OF NOTICE" line
    itself) sits at depth equal to its own natural position in the
    enclosing structure. It does **NOT** take a +1 for "being a
    subdoc"; the header IS the boundary marker that triggers the
    penalty for everything underneath it. A 1st-level subdoc header
    sits at depth 1 (direct child of the agreement title), a
    2nd-level header (subdoc inside subdoc) at depth 2, a 3rd-level
    at depth 3.
  - The subdoc's **body content** absorbs the penalty: each level of
    enclosing subdoc adds +1 to every descendant.

This avoids a "double-count" where a 1st-level subdoc body would
otherwise jump to depth 2 just because the subdoc exists.

## Expected depths

For the **main agreement** body (no subdoc penalty applied):

| element                      | depth |
|------------------------------|-------|
| "MASTER AGREEMENT" (title)   | 0     |
| preamble paragraph           | 1     |
| RECITALS block               | 1     |
| "1. Term and Termination"    | 1     |
| "(a) Initial Term…"          | 2     |
| "(b) Renewal…"               | 2     |
| "2. Definitions"             | 1     |
| "3. Confidentiality"         | 1     |

For **EXHIBIT A** (1st-level subdoc; one enclosing subdoc for the
body, zero for the header):

| element                      | natural | + enclosing subdocs | = depth |
|------------------------------|---------|---------------------|---------|
| "EXHIBIT A — FORM OF NOTICE" | 1       | 0 (header itself)   | 1       |
| "1. Scope of Notice"         | 1       | 1                   | 2       |
| "(a) Required Recipients"    | 2       | 1                   | 3       |
| "(i) by certified mail…"     | 3       | 1                   | 4       |
| "(A) addressed to…"          | 4       | 1                   | 5       |
| "(1) with delivery…"         | 5       | 1                   | 6       |

For **EXHIBIT B > ANNEX I** (subdoc inside subdoc — two enclosing
subdocs apply to anything inside ANNEX I):

| element                                  | natural | + enclosing subdocs | = depth |
|------------------------------------------|---------|---------------------|---------|
| "EXHIBIT B — INTERIM PROCEDURES"         | 1       | 0                   | 1       |
| "1. Procedures" (under Exhibit B)        | 1       | 1                   | 2       |
| "ANNEX I — WIRE INSTRUCTIONS"            | 1       | 1                   | 2       |
| "(a) Bank Details" (inside Annex)        | 2       | 2                   | 4       |
| "(i) Routing"                            | 3       | 2                   | 5       |
| "(A) ACH protocol"                       | 4       | 2                   | 6       |
| "(1) initiation cutoff"                  | 5       | 2                   | 7       |

For **EXHIBIT C > APPENDIX C-1 > SCHEDULE C-1-α** (3rd-level subdoc):

| element                                  | natural | + enclosing subdocs | = depth |
|------------------------------------------|---------|---------------------|---------|
| "EXHIBIT C — FORM OF AMENDMENT"          | 1       | 0                   | 1       |
| "APPENDIX C-1 — TEMPLATE"                | 1       | 1                   | 2       |
| "SCHEDULE C-1-α — SETTLEMENT MATRIX"     | 1       | 2                   | 3       |
| "(a) Tier Calculation"                   | 2       | 3                   | 5       |
| "(i) Annual Volume"                      | 3       | 3                   | 6       |
| "(A) Threshold"                          | 4       | 3                   | 7       |

The "natural" column is the depth the item would have if there were
no enclosing subdocs at all — driven by the marker convention
(`1.` is naturally 1, `(a)` is naturally 2, `(i)` is naturally 3,
`(A)` is naturally 4, `(1)` is naturally 5). The actual depth in
the JSONL is `natural + enclosing-subdocs`.

## Expected JSONL output (truncated example)

`idx` here is a placeholder `N`; in a real run it is the corpus row
index.

```jsonl
{"idx":N,"order":0,"level":0,"span":"MASTER AGREEMENT"}
{"idx":N,"order":1,"level":1,"span":"THIS MASTER AGREEMENT… (preamble)"}
{"idx":N,"order":2,"level":1,"span":"RECITALS\n…"}
{"idx":N,"order":3,"level":1,"span":"1. Term and Termination"}
{"idx":N,"order":4,"level":2,"span":"(a) Initial Term…"}
{"idx":N,"order":5,"level":2,"span":"(b) Renewal…"}
{"idx":N,"order":6,"level":1,"span":"2. Definitions"}
{"idx":N,"order":7,"level":1,"span":"3. Confidentiality"}
{"idx":N,"order":8,"level":1,"span":"EXHIBIT A — FORM OF NOTICE"}
{"idx":N,"order":9,"level":2,"span":"1. Scope of Notice"}
{"idx":N,"order":10,"level":3,"span":"(a) Required Recipients"}
{"idx":N,"order":11,"level":4,"span":"(i) by certified mail…"}
{"idx":N,"order":12,"level":5,"span":"(A) addressed to \"Chief Officer\""}
{"idx":N,"order":13,"level":6,"span":"(1) with delivery confirmation"}
{"idx":N,"order":14,"level":1,"span":"SCHEDULE 2 — DEFINITIONS"}
{"idx":N,"order":15,"level":1,"span":"EXHIBIT B — INTERIM PROCEDURES"}
{"idx":N,"order":16,"level":2,"span":"1. Procedures"}
{"idx":N,"order":17,"level":2,"span":"ANNEX I — WIRE INSTRUCTIONS"}
{"idx":N,"order":18,"level":4,"span":"(a) Bank Details"}
{"idx":N,"order":19,"level":5,"span":"(i) Routing"}
{"idx":N,"order":20,"level":6,"span":"(A) ACH protocol"}
{"idx":N,"order":21,"level":7,"span":"(1) initiation cutoff"}
{"idx":N,"order":22,"level":1,"span":"EXHIBIT C — FORM OF AMENDMENT"}
{"idx":N,"order":23,"level":2,"span":"APPENDIX C-1 — TEMPLATE"}
{"idx":N,"order":24,"level":3,"span":"SCHEDULE C-1-α — SETTLEMENT MATRIX"}
{"idx":N,"order":25,"level":5,"span":"(a) Tier Calculation"}
{"idx":N,"order":26,"level":6,"span":"(i) Annual Volume"}
{"idx":N,"order":27,"level":7,"span":"(A) Threshold"}
```

Note that **`level` is not monotonic** — it can jump up and down
freely as the parser descends into and out of subdocs. What IS
monotonic is `order`, which increments by 1 for every emitted
record regardless of which (sub-)subdoc the record belongs to.

## Notes

- **Subdocuments are detected structurally, not by the word
  "EXHIBIT".** The SEC envelope ("EXHIBIT 10.25") and a real subdoc
  ("EXHIBIT A — FORM OF NOTICE") both use the word, but only the
  latter has descriptive title text and `is_envelope=False`. See
  [`scope_rule.md`](scope_rule.md) §"Detecting a REAL SUBDOCUMENT".
- **Subdoc headers DO descend from the agreement title.** They are
  part of the agreement (referenced by or attached to the body) and
  emit to the JSONL at depth 1+. Only the SEC `<DOCUMENT>` envelope
  — which precedes the title in document order — is dropped.
- **The penalty is additive, not multiplicative.** A node inside
  three enclosing subdocs gets +3, not ×3 or 2^3. The ceiling of 7
  fits the deepest observed nesting in the EX-10 corpus with
  headroom.
- **Subdoc headers do NOT take an extra +1 for being a subdoc.**
  The header sits at the depth it would naturally occupy in the
  enclosing structure; the +1 applies to everything underneath.
  This is the asymmetry described above, and it's what makes a
  1st-level subdoc header land at depth 1 (same as a numbered
  Section of the main agreement) rather than depth 2.
- **A subdoc with no body emits just the header.** SCHEDULE 2 above
  has no content under it — the parser still emits the L1 header
  record but does not synthesise any body. This is correct;
  empty-body subdoc references are common ("SCHEDULE 2 —
  DEFINITIONS [Intentionally Omitted]").
- **Order is global within the idx.** It doesn't reset for
  subdocuments. The first emitted span is `order=0`, then `order=1`,
  …, regardless of which (sub-)subdocument the span belongs to. The
  combination of `order` and `level` lets a downstream consumer
  reconstruct both the linear sequence and the tree hierarchy
  without relying on JSON key ordering.
- **Reconstruction works the same way as for the main agreement.**
  Concatenating all spans for one idx in `order` ascending
  reproduces the source document — subdocument spans included. The
  ≥90% word-coverage gate in `freeze.py` holds at the idx level,
  with subdocs counted as part of the agreement.
- **Bare-identifier subdoc rescue.** If a subdoc header lacks
  descriptive text on its own line ("EXHIBIT A" by itself), the
  parser pairs it with the immediately following descriptive line
  ("FORM OF NOTICE") and treats the two-line pair as a single real
  subdoc. The +1 penalty still applies to everything underneath.
  See [`scope_rule.md`](scope_rule.md) §"Subdoc rescue".
- **Signature pages inside subdocs.** A subdoc may contain its own
  signature page (form attachments often do — "FORM OF NOTICE"
  ends with a signature block to be filled in). Those lines follow
  the same depth-2 contract as in the main agreement, then take the
  subdoc penalty: a `/s/...` line inside a 1st-level subdoc lands
  at depth 3.
- **Subdocument footers are out of scope.** Page-bottom artifacts
  inside any subdoc — banner text, page numbers, exhibit-reference
  footers — are dropped under the same structural rule that drops
  signature-page footers in the main agreement.
- **Depth > 7 is a parser failure.** The combination "fifth-deepest
  item inside three levels of subdocs" reaches depth 7 already
  (`5 + 2 = 7` in the simplest path, `4 + 3 = 7` via the deepest
  subdoc nest). Going deeper would require both deeper structural
  nesting AND deeper subdoc nesting simultaneously — not seen in
  the EX-10 corpus. `freeze.py` rejects any record with `level > 7`
  as evidence the parser captured noise instead of structure.

## Why this stops at depth 7

The rubric ceiling is 7. As the tables above show, the synthetic
example deliberately reaches `7` from two different directions:

  - Depth-5 marker `(1)` inside two enclosing subdocs
    (`EXHIBIT B > ANNEX I`) → `5 + 2 = 7`.
  - Depth-4 marker `(A)` inside three enclosing subdocs
    (`EXHIBIT C > APPENDIX C-1 > SCHEDULE C-1-α`) → `4 + 3 = 7`.

Real EX-10 documents very rarely reach this; we include the
synthetic to verify the parser can produce it correctly when the
source actually contains that structure.

## Order field

`order` is global within the idx — it doesn't reset for subdocuments.
The first emitted span is `order=0` and the counter increments by 1
for each subsequent span, regardless of which (sub-)subdocument the
span belongs to. The combination of `order` and `level` lets a
downstream consumer reconstruct both the linear sequence and the tree
hierarchy without relying on JSON key ordering.

## Depth distribution

Expected shape for the synthetic example above (exact counts):

```
depth 0: 1 record    (the agreement title)
depth 1: 8 records   (preamble, RECITALS, Sections 1/2/3 of main,
                      and the 4 subdoc headers EXHIBIT A, SCHEDULE 2,
                      EXHIBIT B, EXHIBIT C)
depth 2: 5 records   ((a)/(b) under main Section 1, "1. Scope" under
                      EXHIBIT A, "1. Procedures" / ANNEX I under
                      EXHIBIT B, APPENDIX C-1)
depth 3: 2 records   ("(a) Required" under EXHIBIT A, SCHEDULE C-1-α)
depth 4: 2 records   ("(i)" inside EXHIBIT A, "(a) Bank" inside ANNEX I)
depth 5: 2 records   ("(A)" inside EXHIBIT A, "(i) Routing", "(a) Tier")
depth 6: 2 records
depth 7: 2 records
```

Real EX-10 agreements typically peak around depths 2–3 because most
top-level body clauses don't have deep roman/letter nesting, and most
filings carry at most one level of subdocument. A long tail at
depth ≥5 in production output is a signal worth investigating —
either a genuinely deep commercial agreement, or the parser
captured page-layout chrome as structure.

## See also

- [`examples_main_agreement.md`](examples_main_agreement.md) — the
  no-subdocs baseline (depths 0–3 only).
- [`level_rubric.md`](level_rubric.md) — the formal depth contract
  and the JSONL record shape.
- [`scope_rule.md`](scope_rule.md) — the structural test for "real
  subdoc" and the bare-identifier rescue.
- [`freeze_command.md`](freeze_command.md) — the `level ≤ 7` and
  ≥90% reconstruction gates that the freeze validator enforces.

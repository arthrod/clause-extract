# Worked example — agreement with attached subdocuments (depths 0–7)

This example shows how the +1 subdocument penalty propagates. It is
SYNTHETIC — built to exercise every nesting depth from 0 through 7
(the `level` field in JSONL records). The actual SEC corpus rarely
goes deeper than depth 5, but the parser supports the full range.

The example uses the same structural rules as the main-agreement
example: title alone at depth 0, every direct child of the agreement
at depth 1, their children at depth 2, etc. The subdocument penalty
adds +1 to every descendant of a real subdocument (cls in
`{exhibit, schedule, appendix, annex}`, `is_envelope=False`, with
descriptive title text).

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

[1st-level subdoc, second one]
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

## Expected depths

For the **main agreement** body (no subdoc penalty applied):

| element                      | depth |
|------------------------------|-------|
| "MASTER AGREEMENT" (title)   | 0     |
| preamble paragraph           | 1     |
| RECITALS block               | 1     |
| "1. Term and Termination"    | 1     |
| "(a) Initial Term…"          | 2     |
| "2. Definitions"             | 1     |
| "3. Confidentiality"         | 1     |

For **EXHIBIT A** (1st-level subdoc; descendants get +1 penalty):

| element                      | base | +penalty = depth |
|------------------------------|------|------------------|
| "EXHIBIT A — FORM OF NOTICE" |  1   | 1 (header itself at depth 1; the subdoc HEADER doesn't get its own penalty) |
| "1. Scope of Notice"         |  1   | 2 (first body clause inside one subdoc) |
| "(a) Required Recipients"    |  2   | 3                |
| "(i) by certified mail…"     |  3   | 4                |
| "(A) addressed to…"          |  4   | 5                |
| "(1) with delivery…"         |  5   | 6                |

For **EXHIBIT B > ANNEX I** (2nd-level subdoc — Annex inside Exhibit;
descendants get +2):

| element                                  | base | +penalty = depth |
|------------------------------------------|------|------------------|
| "EXHIBIT B — INTERIM PROCEDURES"         |  1   | 1                |
| "1. Procedures" (under Exhibit B)        |  1   | 2                |
| "ANNEX I — WIRE INSTRUCTIONS"            |  1   | 2 (subdoc inside subdoc — header at depth 2) |
| "(a) Bank Details" (inside Annex)        |  2   | 4                |
| "(i) Routing"                            |  3   | 5                |
| "(A) ACH protocol"                       |  4   | 6                |
| "(1) initiation cutoff"                  |  5   | 7                |

For **EXHIBIT C > APPENDIX C-1 > SCHEDULE C-1-α** (3rd-level subdoc):

| element                                  | base | +penalty = depth |
|------------------------------------------|------|------------------|
| "EXHIBIT C — FORM OF AMENDMENT"          |  1   | 1                |
| "APPENDIX C-1 — TEMPLATE"                |  1   | 2                |
| "SCHEDULE C-1-α — SETTLEMENT MATRIX"     |  1   | 3                |
| "(a) Tier Calculation"                   |  2   | 5                |
| "(i) Annual Volume"                      |  3   | 6                |
| "(A) Threshold"                          |  4   | 7                |

## Why this stops at depth 7

The rubric ceiling is 7. The combination "fifth-deepest item inside
three levels of subdocuments" reaches depth 7 already (5 + 2 = 7 in
the simplest path, or 4 + 3 = 7 via the deepest subdoc nest). Going
deeper would require both deeper structural nesting AND deeper subdoc
nesting at the same time, which we have not seen in the EX-10 corpus.

`freeze.py` rejects any record with depth > 7 as evidence the parser
captured noise instead of structure.

## Order field

`order` is global within the idx — it doesn't reset for subdocuments.
The first emitted span is `order=0` and the counter increments by 1
for each subsequent span, regardless of which (sub-)subdocument the
span belongs to. The combination of `order` and `level` lets a
downstream consumer reconstruct both the linear sequence and the tree
hierarchy without relying on JSON key ordering.

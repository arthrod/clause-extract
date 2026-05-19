# Worked example — Employment Agreement (idx=6, ARRIS GROUP)

A bilateral employment contract with no attached subdocuments. This
example sits alongside the Indemnification Agreement
([`examples_main_agreement.md`](examples_main_agreement.md)) to show
that the rubric applies identically to a structurally similar but
semantically different agreement. Depths run 0 through 3.

## Source structure (from the source-of-truth bs4 text, truncated)

```
[SEC envelope, dropped from JSONL]
EX-10.1
Exhibit 10.1

[main agreement]
EMPLOYMENT AGREEMENT
THIS EMPLOYMENT AGREEMENT (the "Agreement"), dated as of 4 January,
2016 is by and between ARRIS GROUP, INC., a Delaware corporation
(the "Company"), and Timothy O'Loughlin ("Executive").

WHEREAS, Executive and the Company want to enter into a written
agreement providing for the terms of Executive's employment by the
Company.

NOW, THEREFORE, in consideration of the foregoing recital and of the
mutual covenants set forth herein, and other good and valuable
consideration, the receipt and sufficiency of which are hereby
acknowledged, the parties agree as follows:

1. Employment. Executive agrees to enter into the continued
   employment of the Company…

2. Compensation.
   (a) Base Salary. During the term of Executive's employment with
       the Company pursuant to this Agreement, the Company shall pay
       to Executive as compensation for his services an annual base
       salary of not less than $450,000…
   (b) Incentive Bonus. During the term of Executive's employment…
       Executive's incentive compensation program shall be determined
       by the Company in its discretion with a target bonus equal to
       80% of Base Salary…

…

IN WITNESS WHEREOF, the parties have executed this Agreement as of
the date first above written.

"Company"
ARRIS GROUP, INC.
By: /s/ R.J. Stanzione
Name: R.J. Stanzione
Title: Chairman & CEO

"Executive"
/s/ Timothy O'Loughlin
Timothy O'Loughlin
```

## Expected JSONL output (truncated example)

```jsonl
{"idx":6,"order":0,"level":0,"span":"EMPLOYMENT AGREEMENT"}
{"idx":6,"order":1,"level":1,"span":"THIS EMPLOYMENT AGREEMENT (the \"Agreement\"), dated as of 4 January, 2016 is by and between ARRIS GROUP, INC., a Delaware corporation (the \"Company\"), and Timothy O'Loughlin (\"Executive\")."}
{"idx":6,"order":2,"level":1,"span":"WHEREAS, Executive and the Company want to enter into a written agreement…\nNOW, THEREFORE, in consideration of the foregoing recital…"}
{"idx":6,"order":3,"level":1,"span":"1. Employment\nExecutive agrees to enter into the continued employment of the Company…"}
{"idx":6,"order":4,"level":1,"span":"2. Compensation."}
{"idx":6,"order":5,"level":2,"span":"(a) Base Salary\n…"}
{"idx":6,"order":6,"level":2,"span":"(b) Incentive Bonus\n…"}
…
{"idx":6,"order":N,"level":1,"span":"IN WITNESS WHEREOF, the parties have executed this Agreement as of the date first above written."}
{"idx":6,"order":N+1,"level":2,"span":"\"Company\"\nARRIS GROUP, INC."}
{"idx":6,"order":N+2,"level":2,"span":"By: /s/ R.J. Stanzione\nName: R.J. Stanzione\nTitle: Chairman & CEO"}
{"idx":6,"order":N+3,"level":2,"span":"\"Executive\""}
{"idx":6,"order":N+4,"level":2,"span":"/s/ Timothy O'Loughlin\nTimothy O'Loughlin"}
```

## Notes

- **The envelope ("Exhibit 10.1") and any registrant cover lines that
  precede the title are dropped from the JSONL** under the
  title-as-root rule, exactly as in the Indemnification example. The
  `EX-10.1` filename tag and the bare "Exhibit 10.1" tag both
  precede the agreement title and so are not descendants of it.
- **Exactly one depth-0 record per idx**: the title alone,
  `"EMPLOYMENT AGREEMENT"`. The preamble paragraph ("THIS EMPLOYMENT
  AGREEMENT…") is its own depth-1 record.
- **Recitals collapse into one or a few depth-1 records.** This
  document has a single WHEREAS clause followed by NOW THEREFORE —
  doc2dict typically groups them as one recital block at depth 1.
- **Top-level body clauses ("1. Employment", "2. Compensation", …)
  are at depth 1.** Their lettered subsections ("(a) Base Salary",
  "(b) Incentive Bonus") are at depth 2.
- **The signature-page operating clause ("IN WITNESS WHEREOF…") is at
  depth 1.** Each signature-page line is at depth 2:
  - `"Company"` party label
  - `ARRIS GROUP, INC.` entity name
  - `By: /s/ R.J. Stanzione` / `Name:` / `Title:` block
  - `"Executive"` role label
  - `/s/ Timothy O'Loughlin` / `Timothy O'Loughlin` lines
- **Quoted role labels (`"Company"`, `"Executive"`) are part of the
  signature block**, not pre-title cover or trailer content. They
  follow the IN WITNESS WHEREOF clause and structurally belong under
  it. They land at depth 2 like the other signature-page lines.
- **No subdocuments are attached.** Employment agreements are
  typically standalone — no Exhibits, Schedules, or Annexes. Depths
  therefore top out at 3 (the `(a)/(b)` items have no further
  `(i)/(A)/(1)` substructure in most sections).
- **Page-bottom artifacts** (the "14" and "15" page numbers visible
  near the IN WITNESS WHEREOF clause in the source) are page-layout
  chrome and are dropped from the JSONL under the signature-page
  footer rule.

## Depth distribution

Expected shape for this agreement (counts approximate):

```
depth 0:   1 record    (the agreement title)
depth 1:  ~12 records  (preamble, recitals block, top-level
                        Sections 1–N, signature-page operating
                        clause "IN WITNESS WHEREOF…")
depth 2:  ~10 records  (lettered subsections (a)/(b)/(c) under
                        Compensation and similar sections, and the
                        four-or-so signature-page lines)
depth 3:    0–2 records (rare — most Compensation subsections don't
                         nest further)
```

The Employment Agreement is structurally close to the Indemnification
Agreement: same depth-0–3 range, same signature-page contract, no
subdocs. What differs is the recital-to-body ratio (employment
agreements typically have one short WHEREAS; indemnification
agreements have several) and the signature-page party labels (here
`"Company"` / `"Executive"` instead of company name / `INDEMNITEE`).
None of that changes the rubric.

## See also

- [`examples_main_agreement.md`](examples_main_agreement.md) — the
  Indemnification Agreement baseline. Same depth contract, different
  document type.
- [`examples_main_agreement_amendment.md`](examples_main_agreement_amendment.md)
  — a much shorter, flatter case (a unilateral plan amendment with
  no signature page).
- [`level_rubric.md`](level_rubric.md) — the formal depth contract.

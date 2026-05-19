# Worked example — Plan Amendment (idx=10, MYLAN N.V.)

A short, structurally flat unilateral amendment to an existing
incentive plan. The whole document is under 3,000 characters. There
are no Recitals in the WHEREAS sense, no Articles, no subdocuments,
and no separate signature page — the corporate adoption clause does
the work of both preamble and execution.

This example exists to illustrate two cases that the rubric calls
out but the longer examples don't fully exercise:

  - **A unilateral instrument** where only one party makes a binding
    promise (the Company amends its own Plan).
  - **A structurally flat document**: the deeper depths simply do
    not appear in the source, and the parser must NOT synthesise
    them (per [`level_rubric.md`](level_rubric.md) §"Common parser
    failure modes").

## Source structure (full document, lightly truncated)

```
[SEC envelope, dropped]
Exhibit
Exhibit 10.1

[pre-title cover, dropped under the title-as-root rule]
MYLAN N.V.

[main agreement]
AMENDMENT TO AMENDED AND RESTATED 2003 LONG-TERM INCENTIVE PLAN

This Amendment (the "Amendment") to the Mylan N.V. Amended and
Restated 2003 Long-Term Incentive Plan (the "Plan") is adopted as
of the 23rd day of February, 2017 (the "Amendment Effective Date")
by Mylan N.V., a public limited liability company (naamloze
vennootschap) incorporated under the laws of the Netherlands (the
"Company"). The Company hereby amends the Plan as follows:

1. Section 6.03(e)(iii) is hereby deleted and replaced with the
   following: Retirement.
   (A) With respect to Options and Stock Appreciation Rights granted
       prior to the Amendment Effective Date, unless otherwise
       provided in an Award Agreement, if a Participant's employment
       by the Company or its Subsidiaries shall terminate because of
       Retirement, any Option and Stock Appreciation Right then held
       by the Participant…
   (B) With respect to Options and Stock Appreciation Rights granted
       on or after the Amendment Effective Date, notwithstanding
       anything in this Article VI to the contrary, the Committee
       may, in its sole discretion, waive the forfeiture period…

2. The clause "the minimum amount of any withholding or other tax
   required by law to be withheld" in the first sentence of
   Section 11.05 shall be deleted and replaced with the clause "up
   to the maximum amount of any withholding or other tax permitted
   by law to be withheld"… All other provisions of the Plan, as
   amended by the foregoing, shall remain in full force and effect
   notwithstanding the adoption of this Amendment.
```

## Expected JSONL output

```jsonl
{"idx":10,"order":0,"level":0,"span":"AMENDMENT TO AMENDED AND RESTATED 2003 LONG-TERM INCENTIVE PLAN"}
{"idx":10,"order":1,"level":1,"span":"This Amendment (the \"Amendment\") to the Mylan N.V. Amended and Restated 2003 Long-Term Incentive Plan (the \"Plan\") is adopted as of the 23rd day of February, 2017 (the \"Amendment Effective Date\") by Mylan N.V., a public limited liability company (naamloze vennootschap) incorporated under the laws of the Netherlands (the \"Company\"). The Company hereby amends the Plan as follows:"}
{"idx":10,"order":2,"level":1,"span":"1. Section 6.03(e)(iii) is hereby deleted and replaced with the following: Retirement."}
{"idx":10,"order":3,"level":2,"span":"(A) With respect to Options and Stock Appreciation Rights granted prior to the Amendment Effective Date…"}
{"idx":10,"order":4,"level":2,"span":"(B) With respect to Options and Stock Appreciation Rights granted on or after the Amendment Effective Date…"}
{"idx":10,"order":5,"level":1,"span":"2. The clause \"the minimum amount of any withholding or other tax required by law to be withheld\" in the first sentence of Section 11.05 shall be deleted and replaced with the clause \"up to the maximum amount of any withholding or other tax permitted by law to be withheld\"… All other provisions of the Plan, as amended by the foregoing, shall remain in full force and effect notwithstanding the adoption of this Amendment."}
```

That's the entire JSONL for this idx: six records, max depth 2.

## Notes

- **"MYLAN N.V." is a pre-title cover line and is dropped.** It
  appears in document order BEFORE the agreement title (the
  amendment headline that follows the registrant name), so under
  the title-as-root rule it cannot be a descendant of the title.
  This is the same mechanism that drops "ULURU Inc." in the
  Indemnification example.
- **Exactly one depth-0 record**: the title
  `"AMENDMENT TO AMENDED AND RESTATED 2003 LONG-TERM INCENTIVE PLAN"`.
  Note the title omits "MYLAN N.V." for the reason above.
- **No WHEREAS recitals.** The adoption paragraph ("This Amendment…
  is adopted as of… by Mylan N.V.…") plays both the preamble role
  (identifying the parties and effective date) and the recital role
  (stating the action being taken). It is one depth-1 record.
- **`(A)` and `(B)` land at depth 2, not depth 4.** The marker
  convention (numbered → lettered → roman → capital → digit) is a
  *style* convention writers follow; it is not the source of truth
  for depth. In this document, `(A)` and `(B)` are direct children
  of `1. Section 6.03(e)(iii)`, so they sit at depth 2 — not at
  depth 4, even though `(A)` is "naturally" the depth-4 marker in
  the corpus-wide convention. **Depth comes from structural
  ancestry, not from marker style.** See
  [`level_rubric.md`](level_rubric.md) §"Worked depth chain".
- **No signature block.** A unilateral plan amendment is adopted by
  the Company; there is no second party to sign and no IN WITNESS
  WHEREOF clause in the source. The deeper depths the rubric
  reserves for signature-page lines simply do not appear — that is
  correct, not a defect. Do NOT synthesise an empty signature block
  to satisfy a "must have depth 2" expectation.
- **The final sentence ("All other provisions of the Plan… shall
  remain in full force and effect…") is part of Section 2, not a
  separate concluding clause.** doc2dict groups it with the
  preceding section text, which is correct.
- **Max depth is 2.** No `(i)/(a)` substructure inside the lettered
  items in this document, so the JSONL stops at depth 2. The
  rubric's "depth ≤ 7" ceiling is upheld trivially.

## Depth distribution

```
depth 0:  1 record  (the title)
depth 1:  3 records (adoption clause, "1. …", "2. …")
depth 2:  2 records ((A) and (B) under section 1)
```

Six records total. The depth profile is a stark contrast to the
Indemnification example (~70 records, depths 0–3 with signature
block) and the License agreement (~300 records with subdocs). The
parser should produce this minimal output without padding — neither
adding empty L1 wrappers nor synthesising a signature placeholder.

## See also

- [`examples_main_agreement.md`](examples_main_agreement.md) — the
  full-shape baseline (Indemnification Agreement with a real
  signature page).
- [`examples_main_agreement_employment.md`](examples_main_agreement_employment.md)
  — a bilateral case (Employment Agreement) with similar depths but
  a real signature page.
- [`level_rubric.md`](level_rubric.md) — the §"Common parser failure
  modes" section calls out "synthesised depth" as a defect; this
  document is the canonical case the rubric is protecting against.

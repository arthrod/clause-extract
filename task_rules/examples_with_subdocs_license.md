# Worked example — License & Option Agreement with subdocs (idx=1, Momenta/CSL)

This is the Momenta Pharmaceuticals / CSL Behring License and Option
Agreement (idx=1 in the corpus, SEC envelope = "Exhibit 10.1").
It is the second frozen baseline in the loop (after idx=0) and
illustrates the +1 subdocument penalty in a **real EX-10 filing**
rather than a synthetic example.

The frozen baseline lives at
`data/auto_parse/level_freeze/frozen/idx_1.jsonl` (298 records).

## Source structure (truncated)

```
[SEC envelope, dropped]
Exhibit
Exhibit 10.1
Confidential Treatment Requested by Momenta Pharmaceuticals, Inc.

[main agreement]
LICENSE AND OPTION AGREEMENT
BY AND BETWEEN
MOMENTA PHARMACEUTICALS, INC.
AND
CSL BEHRING RECOMBINANT FACILITY AG

This License and Option Agreement (the "Agreement"), executed as of
January 4, 2017 (the "Execution Date"), is by and between Momenta
Pharmaceuticals, Inc. ("Momenta") and CSL Behring Recombinant
Facility AG ("CSL").

RECITALS

A. Momenta is performing research in the area of recombinant Fc
   multimeric proteins to treat autoimmune diseases…
B. CSL is engaged in the research, development, manufacture and
   commercialization of biotherapeutic products…
C. Momenta desires to grant to CSL and CSL desires to receive
   exclusive, worldwide licenses to research, develop, manufacture
   and commercialize products…
D. CSL desires to grant to Momenta and Momenta desires to receive,
   by way of alternative consideration…
E. Momenta further desires to receive, and CSL desires to grant an
   option for Momenta to co-promote…

ARTICLE 1. DEFINITIONS
The capitalized terms used in this Agreement (other than the
headings of the Articles and Sections) shall have the following
meanings:
  (a) "50% Co-Funding Election Date" means the date on which
      Momenta exercises its 50% Co-Funding Option…
  (b) "30% Co-Funding Election Date" means the earlier of the date
      on which Momenta exercises its 30% Co-Funding Option…
  …

ARTICLE 2. LICENSE GRANT
  …

…

IN WITNESS WHEREOF, the Parties hereto have set their hand as of
the Execution Date.

MOMENTA PHARMACEUTICALS, INC.
By: /s/ Craig A. Wheeler
Name: Craig A. Wheeler
Title: President and Chief Executive Officer

CSL BEHRING RECOMBINANT FACILITY AG by its duly authorized attorney
By: /s/ David Lamont
Name: David Lamont
Title: Chief Financial Officer

[attached subdocs — these come AFTER the signature block but are
 attached to the agreement, so they're in scope with +1 penalty]
SCHEDULE 1.33 CALCULATION OF LABOR COSTS, EXPENSE ALLOCATION AND
              RELATED MATTERS
  [***]   (confidential treatment marker — the body is redacted)

SCHEDULE 1.93 MOMENTA PATENT RIGHTS
  [***]

SCHEDULE 1.135 TECHNOLOGY TRANSFER PLAN – OUTLINE OF CMC ASPECTS
  [***]
  SCHEDULE 5.2(b) OUTLINE OF INITIAL DEVELOPMENT PLAN FOR FIRST PRODUCT
  …

EXHIBIT 8.2 INITIAL PRESS RELEASE
  MOMENTA PHARMACEUTICALS, INC.  675 WEST KENDALL STREET
  T: 617.491.9700  WWW.MOMENTAPHARMA.COM
  …
```

## Expected JSONL output (excerpted from the frozen baseline)

```jsonl
{"idx":1,"order":0,"level":0,"span":"LICENSE AND OPTION AGREEMENT"}
{"idx":1,"order":1,"level":1,"span":"This License and Option Agreement (the \"Agreement\"), executed as of January 4, 2017 (the \"Execution Date\")…"}
{"idx":1,"order":2,"level":1,"span":"RECITALS"}
{"idx":1,"order":3,"level":1,"span":"A.Momenta is performing research in the area of recombinant Fc multimeric proteins to treat autoimmune diseases…"}
{"idx":1,"order":4,"level":1,"span":"B.CSL is engaged in the research, development, manufacture and commercialization of biotherapeutic products…"}
…
{"idx":1,"order":8,"level":1,"span":"ARTICLE 1. DEFINITIONS\nThe capitalized terms used in this Agreement…"}
{"idx":1,"order":9,"level":2,"span":"(a)    in respect of the 50% Co-Funding Option, the date on which Momenta exercises its 50% Co-Funding Option…"}
{"idx":1,"order":10,"level":2,"span":"(b)    in respect of the 30% Co-Funding Option, the earlier of the date on which Momenta exercises…"}
…
{"idx":1,"order":285,"level":1,"span":"IN WITNESS WHEREOF, the Parties hereto have set their hand as of the Execution Date."}
{"idx":1,"order":286,"level":2,"span":"MOMENTA PHARMACEUTICALS, INC."}
{"idx":1,"order":287,"level":2,"span":"By: /s/ Craig A. Wheeler"}
{"idx":1,"order":288,"level":2,"span":"Name: Craig A. Wheeler"}
{"idx":1,"order":289,"level":2,"span":"Title: President and Chief Executive Officer"}
{"idx":1,"order":290,"level":2,"span":"CSL BEHRING RECOMBINANT FACILITY AG by its duly authorized attorney"}
{"idx":1,"order":291,"level":2,"span":"By: /s/ David Lamont"}
{"idx":1,"order":292,"level":2,"span":"Name: David Lamont"}
{"idx":1,"order":293,"level":2,"span":"Title: Chief Financial Officer"}
{"idx":1,"order":294,"level":1,"span":"SCHEDULE 1.33 CALCULATION OF LABOR COSTS, EXPENSE ALLOCATION AND RELATED MATTERS\n[***]"}
{"idx":1,"order":295,"level":1,"span":"SCHEDULE 1.93 MOMENTA PATENT RIGHTS\n[***]"}
{"idx":1,"order":296,"level":1,"span":"SCHEDULE 1.135 TECHNOLOGY TRANSFER PLAN – OUTLINE OF CMC ASPECTS\n[***]\nSCHEDULE 5.2(b)\nOUTLINE OF INITIAL DEVELOPMENT PLAN FOR FIRST PRODUCT"}
{"idx":1,"order":297,"level":1,"span":"EXHIBIT 8.2 INITIAL PRESS RELEASE\nMOMENTA PHARMACEUTICALS, INC.        675 WEST KENDALL STREET…"}
```

## Notes

- **`ARTICLE 1. DEFINITIONS` is at depth 1.** This agreement uses
  ARTICLE-level numbering (not pure top-level Sections), but
  ARTICLE 1 is still a direct child of the title — so it sits at
  depth 1, the same depth as a top-level Section would in the
  Indemnification example. Depth is set by ancestry from the title,
  not by what the clause is named.
- **Recital letters `A.` / `B.` / `C.` / `D.` / `E.` are at depth
  1.** In this document the RECITALS block is split into individual
  lettered recitals at the same depth as the block header. (Some
  agreements collapse all recitals into one depth-1 record; this
  one keeps them separate. Either is rubric-compliant — the depth
  is the same.)
- **The frozen baseline emits SCHEDULEs and the EXHIBIT as L1
  records with redacted bodies.** Each SCHEDULE / EXHIBIT header
  passes the real-subdoc test (`cls in {exhibit, schedule, ...}`,
  `is_envelope=False`, descriptive title) and is in scope. The body
  of most subdocs is just `[***]` (confidential treatment marker) —
  the substantive content was redacted before filing. There's no
  deep body content to take the +1 penalty here, so depth 2 / 3
  records inside the subdocs are mostly absent for this document.
- **The subdoc penalty IS active.** It just doesn't materialize as
  visible records because the subdoc bodies are `[***]`. If the
  bodies were present, a `1. Scope` line inside SCHEDULE 1.33 would
  land at depth 2 (natural 1 + 1 enclosing subdoc), an `(a)` inside
  it at depth 3, and so on through the rubric in
  [`examples_with_subdocs.md`](examples_with_subdocs.md).
- **The `[***]` markers are part of the span**, appended to the
  schedule header as the body content. They are NOT separate
  records and they are NOT trailer content — they ARE the
  (redacted) subdoc body.
- **The signature page contains two signatories.** Each party's
  block (entity name → `By:` → `Name:` → `Title:`) emits as
  separate depth-2 lines under the depth-1 IN WITNESS WHEREOF
  operating clause. CSL's first line is longer because it includes
  "by its duly authorized attorney" — that's still one signature-
  page line, kept at depth 2.
- **The "Confidential Treatment Requested" cover wedge above the
  title is dropped** under the title-as-root rule — it appears
  before "LICENSE AND OPTION AGREEMENT" in document order.
- **Composite SCHEDULE span (order=296).** The frozen baseline emits
  one record covering "SCHEDULE 1.135 …" *and* the immediately
  following "SCHEDULE 5.2(b) …" because doc2dict grouped them
  together in the source HTML. This is a quirk of how doc2dict
  segments adjacent schedule headers when no body content separates
  them — the record is correct under the reconstruction goal (the
  text concatenates back) but it does mean two logical subdocs
  share one span.

## Depth distribution

For the frozen idx=1 baseline (298 records):

```
depth 0:   1 record    (title)
depth 1: ~70 records   (preamble + RECITALS block + lettered
                        recitals A–E + ~16 ARTICLEs + IN WITNESS
                        WHEREOF + 4 attached subdoc headers)
depth 2: ~220 records  (Sections under Articles, lettered
                        subsections, signature-page lines)
depth 3:   ~7 records  (deeper roman/letter items inside
                        Articles 6/8 — arbitration mechanics)
```

The agreement is large (251K source chars, 298 records) but the
depth profile is moderate: most content is at depth 2, with
ARTICLE-level structure at depth 1 and limited roman/letter nesting
at depth 3. The subdoc penalty is theoretically active for the four
trailing attachments but doesn't manifest because their bodies are
redacted.

## See also

- [`examples_with_subdocs.md`](examples_with_subdocs.md) — the
  synthetic case that exercises depths 4–7 under the +1 penalty,
  showing what `idx=1`'s subdocs WOULD look like if their bodies
  were not redacted.
- [`examples_with_subdocs_investment.md`](examples_with_subdocs_investment.md)
  — a different real-document case (Pandora/KKR Investment
  Agreement) with non-redacted ANNEX content, a TOC drop, and a
  post-signature press-release trailer drop.
- [`scope_rule.md`](scope_rule.md) — the structural test for "real
  subdoc" (which all four trailing SCHEDULEs and the EXHIBIT pass).
- [`level_rubric.md`](level_rubric.md) — the formal depth contract.

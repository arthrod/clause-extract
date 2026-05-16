# Worked example — main agreement, no subdocuments

This is the ULURU Inc. Indemnification Agreement (idx=0 in our corpus,
SEC envelope = "EXHIBIT 10.25"). It has NO attached subdocuments. Under
the rubric (title alone at depth 0; preamble, recitals, every top-level
body clause, and the signature-page operating clause at depth 1; their
direct children — including the signature-page lines — at depth 2;
etc.), depths run 0 through 3. Concatenating the spans below in
`order` ascending reconstructs the source.

## Source structure (from the source-of-truth bs4 text)

```
[SEC envelope, dropped from JSONL]
EXHIBIT 10.25
ULURU Inc.

[main agreement]
INDEMNIFICATION AGREEMENT
THIS INDEMNIFICATION AGREEMENT (the "Agreement") is made and entered
into as of February 27, 2017 between ULURU Inc., a Nevada corporation
(the "Company"), and Vaidehi Shah ("Indemnitee").

WITNESSETH THAT:
WHEREAS, highly competent persons have become more reluctant…
WHEREAS, the Board of Directors of the Company…
…
NOW, THEREFORE, in consideration of Indemnitee's agreement…

1. Indemnity of Indemnitee. The Company hereby agrees to hold harmless…
  (a) Proceedings Other Than Proceedings by or in the Right of the Company…
  (b) Proceedings by or in the Right of the Company…
  (c) Indemnification under NRS 78.138…
  (d) Indemnification for Expenses of a Party Who is Wholly or Partly Successful…

2. Additional Indemnity. In addition to, and without regard to any limitation…

3. Contribution.
  (a) Whether or not the indemnification provided in Sections 1 and 2 hereof…
  (b) Without diminishing or impairing the obligations of the Company…
  (c) The Company hereby agrees to fully indemnify and hold Indemnitee harmless…
  (d) To the fullest extent permissible under applicable law…

…

6. Procedures and Presumptions for Determination of Entitlement to Indemnification.
  (a) To obtain indemnification under this Agreement, Indemnitee shall submit…
  (b) Upon written request by Indemnitee for indemnification…
    (i) by a majority vote of the disinterested Directors…
  (c) Notwithstanding anything to the contrary…

…

IN WITNESS WHEREOF, the parties hereto have executed this Indemnification
Agreement as of the date first written above.

ULURU Inc.
By: /s/ Terrance K. Wallberg
Name: Terrance K. Wallberg
Title: Vice President and Chief Financial Officer

INDEMNITEE
/s/ Vaidehi Shah
Vaidehi Shah
Address:
```

## Expected JSONL output (truncated example)

```jsonl
{"idx":0,"order":0,"level":0,"span":"INDEMNIFICATION AGREEMENT"}
{"idx":0,"order":1,"level":1,"span":"THIS INDEMNIFICATION AGREEMENT (the \"Agreement\") is made and entered into as of February 27, 2017 between ULURU Inc., a Nevada corporation (the \"Company\"), and Vaidehi Shah (\"Indemnitee\")."}
{"idx":0,"order":2,"level":1,"span":"WITNESSETH THAT:\nWHEREAS, highly competent persons have become more reluctant…\nWHEREAS, the Board of Directors of the Company…\nNOW, THEREFORE, in consideration of Indemnitee's agreement…"}
{"idx":0,"order":3,"level":1,"span":"1. Indemnity of Indemnitee\nThe Company hereby agrees to hold harmless and indemnify Indemnitee…"}
{"idx":0,"order":4,"level":2,"span":"(a) Proceedings Other Than Proceedings by or in the Right of the Company\n…"}
{"idx":0,"order":5,"level":2,"span":"(b) Proceedings by or in the Right of the Company\n…"}
{"idx":0,"order":6,"level":2,"span":"(c) Indemnification under NRS 78.138\n…"}
{"idx":0,"order":7,"level":2,"span":"(d) Indemnification for Expenses of a Party Who is Wholly or Partly Successful\n…"}
{"idx":0,"order":8,"level":1,"span":"2. Additional Indemnity\n…"}
{"idx":0,"order":9,"level":1,"span":"3. Contribution."}
{"idx":0,"order":10,"level":2,"span":"(a) Whether or not the indemnification provided in Sections 1 and 2 hereof…"}
…
{"idx":0,"order":N,"level":3,"span":"(i) by a majority vote of the disinterested Directors…"}
…
{"idx":0,"order":N+k-1,"level":1,"span":"IN WITNESS WHEREOF, the parties hereto have executed this Indemnification Agreement as of the date first written above."}
{"idx":0,"order":N+k,"level":2,"span":"ULURU Inc.\nBy: /s/ Terrance K. Wallberg\nName: Terrance K. Wallberg\nTitle: Vice President and Chief Financial Officer"}
{"idx":0,"order":N+k+1,"level":2,"span":"INDEMNITEE"}
{"idx":0,"order":N+k+2,"level":2,"span":"/s/ Vaidehi Shah"}
{"idx":0,"order":N+k+3,"level":2,"span":"Vaidehi Shah\nAddress:"}
```

## Notes

- **Neither "EXHIBIT 10.25" nor the registrant cover line "ULURU Inc."
  (which precedes the agreement title) appears in the JSONL.** They
  are dropped via two distinct mechanisms:
  - "EXHIBIT 10.25" is the SEC filing envelope; the parser detects it
    via `is_envelope=true` in the parquet and drops it from the JSONL
    while keeping it in the parquet for audit.
  - "ULURU Inc." is dropped under the **title-as-root** rule (see
    `level_rubric.md`): it precedes the title in document order, so it
    cannot be a descendant of the title and therefore cannot be part
    of the agreement.
- **Exactly one depth-0 record per idx**, and it is the *title alone*:
  `"INDEMNIFICATION AGREEMENT"`. The preamble paragraph
  (`"THIS INDEMNIFICATION AGREEMENT (the 'Agreement') is made…"`) is
  its own depth-1 record — a child of the agreement, not part of the
  title.
- The WHEREAS recitals together form one depth-1 record (or several
  depth-1 records, depending on how doc2dict groups them — either way
  they sit at depth 1 as direct children of the agreement).
- Top-level body clauses (`1. Indemnity`, `2. Additional Indemnity`,
  `3. Contribution`, `6. Procedures…`) are at **depth 1**.
- Their lettered subsections (`(a)`, `(b)`, `(c)`, `(d)`) are at
  **depth 2**.
- Roman / capital-letter / digit items (`(i)`, `(A)`, `(1)`) are at
  **depth 3** when they appear inside an `(a)`-style subsection.
- The signature-page **operating clause** ("IN WITNESS WHEREOF, the
  parties hereto have executed this Agreement…") is at **depth 1** —
  it is the signature page's header, a direct child of the agreement.
- Each signature-page **line** (party name label, `/s/...` line,
  `By:`/`Name:`/`Title:` block, `INDEMNITEE` role label, `Address:`)
  is at **depth 2** — a child of the signature-page operating clause.
  Lines are flat siblings at depth 2; document order tells you which
  lines belong to which party (a party label precedes its `/s/`,
  name, title, address).
- In modern agreements that **lack** the "IN WITNESS WHEREOF…"
  operating clause, signature-page lines remain at **depth 2**. The
  conceptual depth-1 parent (the signature page itself) is simply
  absent in the source; the parser still emits the lines at depth 2
  to keep the signature-page depth contract consistent across the
  corpus. This is a theoretical compromise: we hold the depth steady
  so a downstream classifier can target depth-2 uniformly across
  documents. It is not a classification exercise here — the parser
  does not label lines as "signature" vs "notary" vs "witness"; it
  just preserves them at depth 2.
- **Signature-page footers are out of scope.** Page-bottom artifacts
  inside the signature page — `[Signature Page Follows]`,
  `-- Signature Page --` banners, page numbers, exhibit-reference
  footers — do not descend from the title structurally; they are
  page-layout chrome injected by the typesetter and are dropped from
  the JSONL.
- `order` is 0-indexed and monotonic within idx=0 — first emitted
  record gets `order=0`, second gets `order=1`, and so on.

## Depth distribution

Expected shape for this agreement (counts approximate):

```
depth 0: 1 record    (the agreement title)
depth 1: ~20 records (preamble, recitals, top-level Sections 1–21,
                      signature-page operating clause)
depth 2: ~40 records (lettered subsections (a)/(b)/(c)/(d) and the
                      signature-page lines — regardless of whether
                      the operating clause is present)
depth 3: ~5 records  (sub-sub items like (i)/(ii))
```

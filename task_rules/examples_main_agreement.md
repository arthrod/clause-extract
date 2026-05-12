# Worked example — main agreement, no subdocuments

This is the ULURU Inc. Indemnification Agreement (idx=0 in our corpus,
SEC envelope = "EXHIBIT 10.25"). It has NO attached subdocuments. Under
the rubric (title alone at depth 0; preamble, recitals, every top-level
body clause, and the signature block at depth 1; their direct children
at depth 2; etc.), depths run 0 through 3. Concatenating the spans
below in `order` ascending reconstructs the source.

## Source structure (from the source-of-truth bs4 text)

```
[SEC envelope, dropped from JSONL]
EXHIBIT 10.25

[main agreement]
ULURU Inc.
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
{"idx":0,"order":0,"level":1,"span":"ULURU Inc."}
{"idx":0,"order":1,"level":0,"span":"INDEMNIFICATION AGREEMENT"}
{"idx":0,"order":2,"level":1,"span":"THIS INDEMNIFICATION AGREEMENT (the \"Agreement\") is made and entered into as of February 27, 2017 between ULURU Inc., a Nevada corporation (the \"Company\"), and Vaidehi Shah (\"Indemnitee\")."}
{"idx":0,"order":3,"level":1,"span":"WITNESSETH THAT:\nWHEREAS, highly competent persons have become more reluctant…\nWHEREAS, the Board of Directors of the Company…\nNOW, THEREFORE, in consideration of Indemnitee's agreement…"}
{"idx":0,"order":4,"level":1,"span":"1. Indemnity of Indemnitee\nThe Company hereby agrees to hold harmless and indemnify Indemnitee…"}
{"idx":0,"order":5,"level":2,"span":"(a) Proceedings Other Than Proceedings by or in the Right of the Company\n…"}
{"idx":0,"order":6,"level":2,"span":"(b) Proceedings by or in the Right of the Company\n…"}
{"idx":0,"order":7,"level":2,"span":"(c) Indemnification under NRS 78.138\n…"}
{"idx":0,"order":8,"level":2,"span":"(d) Indemnification for Expenses of a Party Who is Wholly or Partly Successful\n…"}
{"idx":0,"order":9,"level":1,"span":"2. Additional Indemnity\n…"}
{"idx":0,"order":10,"level":1,"span":"3. Contribution."}
{"idx":0,"order":11,"level":2,"span":"(a) Whether or not the indemnification provided in Sections 1 and 2 hereof…"}
…
{"idx":0,"order":N,"level":3,"span":"(i) by a majority vote of the disinterested Directors…"}
…
{"idx":0,"order":N+k,"level":1,"span":"ULURU Inc.\nBy: /s/ Terrance K. Wallberg\nName: Terrance K. Wallberg\nTitle: Vice President and Chief Financial Officer"}
{"idx":0,"order":N+k+1,"level":1,"span":"INDEMNITEE"}
{"idx":0,"order":N+k+2,"level":1,"span":"/s/ Vaidehi Shah"}
{"idx":0,"order":N+k+3,"level":1,"span":"Vaidehi Shah\nAddress:"}
```

Notes:

- **"EXHIBIT 10.25" is NOT in the JSONL.** It's the SEC envelope, kept
  in the parquet (`is_envelope=true`) but dropped from JSONL.
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
- Signature-block parts (party name labels, `/s/...` lines,
  `By:`/`Name:`/`Title:` blocks, `INDEMNITEE` role label) are all at
  **depth 1**.
- `order` is 0-indexed and monotonic within idx=0 — first emitted
  record gets `order=0`, second gets `order=1`, and so on.

## Depth distribution

Expected shape for this agreement (counts approximate):

```
depth 0: 1 record   (the agreement title)
depth 1: ~20 records (party block, preamble, recitals, top-level
                      Sections 1–10, signature parts)
depth 2: ~40 records (lettered subsections (a)/(b)/(c)/(d))
depth 3: ~5 records  (sub-sub items like (i)/(ii))
```

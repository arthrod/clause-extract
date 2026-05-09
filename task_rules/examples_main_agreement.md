# Worked example — main agreement, no subdocuments

This is the ULURU Inc. Indemnification Agreement (idx=0 in our corpus,
SEC envelope = "EXHIBIT 10.25"). It has NO attached subdocuments, so no
penalty applies — levels run 0 through 4.

## Source structure (from the source-of-truth bs4 text)

```
[SEC envelope, dropped from JSONL]
EXHIBIT 10.25

[main contract]
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

## Expected JSONL output (66 records, current frozen baseline for idx=0)

```jsonl
{"idx":0,"level":1,"span":"ULURU Inc."}
{"idx":0,"level":0,"span":"INDEMNIFICATION AGREEMENT\nTHIS INDEMNIFICATION AGREEMENT (the \"Agreement\") is made and entered into as of February 27, 2017 between ULURU Inc., a Nevada corporation (the \"Company\"), and Vaidehi Shah (\"Indemnitee\")."}
{"idx":0,"level":1,"span":"WITNESSETH THAT:\nWHEREAS, highly competent persons…"}
{"idx":0,"level":2,"span":"1. Indemnity of Indemnitee\nThe Company hereby agrees to hold harmless…"}
{"idx":0,"level":3,"span":"(a) Proceedings Other Than Proceedings by or in the Right of the Company\n…"}
{"idx":0,"level":3,"span":"(b) Proceedings by or in the Right of the Company\n…"}
{"idx":0,"level":3,"span":"(c) Indemnification under NRS 78.138\n…"}
{"idx":0,"level":3,"span":"(d) Indemnification for Expenses of a Party Who is Wholly or Partly Successful\n…"}
{"idx":0,"level":2,"span":"2. Additional Indemnity\n…"}
{"idx":0,"level":2,"span":"3. Contribution."}
{"idx":0,"level":3,"span":"(a) Whether or not the indemnification provided in Sections 1 and 2 hereof…"}
…
{"idx":0,"level":4,"span":"(i) by a majority vote of the disinterested Directors…"}
…
{"idx":0,"level":1,"span":"ULURU Inc.\nBy: /s/ Terrance K. Wallberg\n…"}
{"idx":0,"level":1,"span":"INDEMNITEE"}
{"idx":0,"level":1,"span":"/s/ Vaidehi Shah"}
{"idx":0,"level":1,"span":"Vaidehi Shah\nAddress:"}
```

Note:

- "EXHIBIT 10.25" is **NOT** in the JSONL. It's the SEC envelope, kept
  in the parquet (`is_envelope=true`) but dropped from JSONL.
- The agreement title + preamble is one record at level 0.
- Each WHEREAS recital is part of the level-1 WITNESSETH block (or its
  own level-1 record, depending on how doc2dict groups them).
- The signature block parts are level 1.

## Level distribution

```
level 0: 1 record   (the agreement)
level 1: 7 records  (party block, recitals, signature)
level 2: 13 records (numbered Sections)
level 3: 38 records (lettered subsections)
level 4: 7 records  (sub-sub items)
total:   66 records
```

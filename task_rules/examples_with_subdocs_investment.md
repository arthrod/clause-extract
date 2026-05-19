# Worked example — Investment Agreement with TOC + ANNEXes (idx=2, Pandora/KKR)

This is the Pandora Media / KKR Classic Investors Investment
Agreement (idx=2, SEC envelope = "Exhibit"). It exercises four
mechanisms at once in a single real document:

  1. **ARTICLE → Section** nesting in the main body (deeper than
     the simple numbered-Section structure of the Indemnification
     and Employment examples).
  2. A multi-page **TABLE OF CONTENTS** between the preamble and
     the body — layout chrome that gets dropped.
  3. Multiple **attached subdocuments** (ANNEX I, ANNEX II, ANNEX
     III, EXHIBIT A), one of which (ANNEX II) is a complete
     separate agreement (the Registration Rights Agreement) with
     its own structure.
  4. A **post-signature press-release trailer** ("ABOUT PANDORA",
     "ABOUT KKR", contact blocks) that must NOT be emitted.

It's a useful counterpart to the Momenta/CSL license
([`examples_with_subdocs_license.md`](examples_with_subdocs_license.md))
because the subdocs here have real, non-redacted bodies — so the
+1 penalty is visible.

## Source structure (heavily truncated — full doc is ~372K chars)

```
[SEC envelope, dropped]
Exhibit

[pre-title cover, dropped]
INVESTMENT AGREEMENT by and among
PANDORA MEDIA, INC., KKR CLASSIC INVESTORS LLC
and THE OTHER PURCHASERS HERETO
Dated as of May 8, 2017

[main agreement]
INVESTMENT AGREEMENT

(preamble paragraph identifying the parties and the date)

[TABLE OF CONTENTS — layout chrome, dropped from JSONL]
TABLE OF CONTENTS                       PAGE
ARTICLE I  DEFINITIONS                    1
  Section 1.01 Definitions                1
ARTICLE II  PURCHASE AND SALE            11
  Section 2.01 Purchase and Sale         11
  Section 2.02 Initial Closing           11
  Section 2.03 Additional Closings       12
ARTICLE III  REPRESENTATIONS AND WARRANTIES OF THE COMPANY  12
  Section 3.01 Organization; Standing    13
  …                                       …
[end TOC]

ARTICLE I
DEFINITIONS
  Section 1.01 Definitions
    "Affiliate" means, with respect to any Person…
    "Agreement" means this Investment Agreement…
    …

ARTICLE II
PURCHASE AND SALE
  Section 2.01 Purchase and Sale
    Subject to the terms and conditions of this Agreement…
  Section 2.02 Initial Closing
    …
  Section 2.03 Additional Closings
    …

ARTICLE III
REPRESENTATIONS AND WARRANTIES OF THE COMPANY
  Section 3.01 Organization; Standing
    The Company is a corporation duly organized…
    (a) The Company is in good standing…
    (b) Each subsidiary of the Company…
        (i) is duly organized…
        (ii) is in good standing…
  …

…

IN WITNESS WHEREOF, the parties hereto have caused this Agreement
to be duly executed as of the date first written above.

PANDORA MEDIA, INC.
By: /s/ …
Name: …
Title: …

KKR CLASSIC INVESTORS LLC
By: /s/ …
Name: …
Title: …

[attached subdocs — in scope, +1 penalty applies]
ANNEX I
FORM OF CERTIFICATE OF DESIGNATIONS
  …

ANNEX II
REGISTRATION RIGHTS AGREEMENT
REGISTRATION RIGHTS AGREEMENT
by and among PANDORA MEDIA, INC.
and EACH OF THE INVESTORS PARTY HERETO
  ARTICLE 1  DEFINITIONS
    Section 1.1 Defined Terms
    …
  ARTICLE 2  REGISTRATION RIGHTS
    Section 2.1 Demand Registrations
    …

EXHIBIT A
DEFINED TERMS
  "Affiliate", "Agreement", … (the defined-terms appendix)

ANNEX III
ANNOUNCEMENT
  (joint press-release text bundled with the agreement)

[post-signature trailer — out of scope, NOT emitted to JSONL]
ABOUT PANDORA
Pandora is the world's most powerful music discovery platform…
ABOUT KKR
KKR is a leading global investment firm…
###
Contacts:
Dominic Paschel, 510-842-6960
Corporate Finance & Investor Relations
investor@pandora.com
or Stephanie Barnes, 415-722-0883
Pandora Corporate Communications
sbarnes@pandora.com
or For KKR:
Kristi Huller or Cara Kleiman, 212-750-8300
media@kkr.com
```

Note: the "ABOUT PANDORA" / "ABOUT KKR" / "Contacts:" block at the
very end is part of a **press-release trailer**, not part of ANNEX
III's Announcement body. ANNEX III is a real subdoc; the trailer is
not. The structural rule cuts cleanly between them.

## Expected JSONL output (truncated, with depth annotations)

```jsonl
{"idx":2,"order":0,"level":0,"span":"INVESTMENT AGREEMENT"}
{"idx":2,"order":1,"level":1,"span":"by and among\nPANDORA MEDIA, INC.,\nKKR CLASSIC INVESTORS LLC\nand THE OTHER PURCHASERS HERETO\nDated as of May 8, 2017\n…"}
…
{"idx":2,"order":K,"level":1,"span":"ARTICLE I\nDEFINITIONS"}
{"idx":2,"order":K+1,"level":2,"span":"Section 1.01 Definitions\n…"}
{"idx":2,"order":K+2,"level":1,"span":"ARTICLE II\nPURCHASE AND SALE"}
{"idx":2,"order":K+3,"level":2,"span":"Section 2.01 Purchase and Sale\n…"}
{"idx":2,"order":K+4,"level":2,"span":"Section 2.02 Initial Closing\n…"}
{"idx":2,"order":K+5,"level":2,"span":"Section 2.03 Additional Closings\n…"}
{"idx":2,"order":K+6,"level":1,"span":"ARTICLE III\nREPRESENTATIONS AND WARRANTIES OF THE COMPANY"}
{"idx":2,"order":K+7,"level":2,"span":"Section 3.01 Organization; Standing\n…"}
{"idx":2,"order":K+8,"level":3,"span":"(a) The Company is in good standing…"}
{"idx":2,"order":K+9,"level":3,"span":"(b) Each subsidiary of the Company…"}
{"idx":2,"order":K+10,"level":4,"span":"(i) is duly organized…"}
{"idx":2,"order":K+11,"level":4,"span":"(ii) is in good standing…"}
…
{"idx":2,"order":S,"level":1,"span":"IN WITNESS WHEREOF, the parties hereto have caused this Agreement to be duly executed as of the date first written above."}
{"idx":2,"order":S+1,"level":2,"span":"PANDORA MEDIA, INC."}
{"idx":2,"order":S+2,"level":2,"span":"By: /s/ …\nName: …\nTitle: …"}
{"idx":2,"order":S+3,"level":2,"span":"KKR CLASSIC INVESTORS LLC"}
{"idx":2,"order":S+4,"level":2,"span":"By: /s/ …\nName: …\nTitle: …"}
{"idx":2,"order":T,"level":1,"span":"ANNEX I\nFORM OF CERTIFICATE OF DESIGNATIONS"}
…                                              ← ANNEX I body at L2 (natural 1 + 1)
{"idx":2,"order":U,"level":1,"span":"ANNEX II\nREGISTRATION RIGHTS AGREEMENT"}
{"idx":2,"order":U+1,"level":2,"span":"ARTICLE 1\nDEFINITIONS"}     ← natural 1 + 1 = 2
{"idx":2,"order":U+2,"level":3,"span":"Section 1.1 Defined Terms\n…"} ← natural 2 + 1 = 3
{"idx":2,"order":U+3,"level":2,"span":"ARTICLE 2\nREGISTRATION RIGHTS"}
{"idx":2,"order":U+4,"level":3,"span":"Section 2.1 Demand Registrations\n…"}
…
{"idx":2,"order":V,"level":1,"span":"EXHIBIT A\nDEFINED TERMS"}
…
{"idx":2,"order":W,"level":1,"span":"ANNEX III\nANNOUNCEMENT"}
{"idx":2,"order":W+1,"level":2,"span":"(joint press-release text bundled with the agreement)"}
```

Records for the post-signature press-release boilerplate ("ABOUT
PANDORA", "ABOUT KKR", "Contacts:") are NOT in the JSONL — they're
written to the parquet with `scope="trailer"` only.

## Notes

- **`INVESTMENT AGREEMENT` is the title at depth 0**, exactly one
  record. The lines "by and among / PANDORA MEDIA, INC., / KKR
  CLASSIC INVESTORS LLC / …" that follow are preamble at depth 1;
  doc2dict may emit them as one combined span or as several
  consecutive L1 spans depending on the HTML structure. Either is
  rubric-compliant.
- **The TABLE OF CONTENTS is dropped, structurally.** A TOC is
  page-layout chrome inserted by the typesetter to help readers
  jump around the document; it is not a descendant of the title in
  the agreement's logical tree. The titles it names (ARTICLE I,
  Section 1.01, …) appear later in the source as their actual
  body — those are what gets emitted. The TOC entries themselves
  are dropped from the JSONL and retained in the parquet with
  `scope="trailer"` for audit.
- **`ARTICLE I` is at depth 1 and `Section 1.01` is at depth 2.**
  Same rule as the Momenta/CSL example: ARTICLE is a direct child
  of the title, Section is a direct child of ARTICLE. The
  "natural" `(a)` lettered marker that would land at depth 2 in a
  flat agreement ends up at depth 3 here because it sits under
  Section, which sits under ARTICLE.
- **Roman items `(i)` / `(ii)` reach depth 4** in the
  Representations section. That's still well under the depth-7
  ceiling. The chain is `title (0) → ARTICLE III (1) → Section
  3.01 (2) → (a)/(b) (3) → (i)/(ii) (4)`.
- **The subdoc penalty kicks in for ANNEX I / ANNEX II / EXHIBIT A
  / ANNEX III.** Each header sits at depth 1 (direct child of the
  title). Their body content is depth-shifted by +1. So:
  - `ARTICLE 1` *inside ANNEX II* lands at depth **2**, not depth 1.
  - `Section 1.1` *inside ANNEX II's ARTICLE 1* lands at depth **3**.
  - A lettered `(a)` inside that Section would land at depth **4**.
  - Even though ANNEX II is itself a full agreement with its own
    title-like header, in this document's tree it is a subdoc of
    the main Investment Agreement.
- **ANNEX II contains a complete separate agreement.** The
  "REGISTRATION RIGHTS AGREEMENT by and among PANDORA MEDIA, INC.
  and EACH OF THE INVESTORS PARTY HERETO" header inside ANNEX II
  is NOT a separate idx — it's part of idx=2, just deeper in the
  tree. The rubric does not split one filing into multiple idxs.
- **EXHIBIT A — DEFINED TERMS** is an in-line defined-terms appendix.
  It is a real subdoc (descriptive title) and emits at L1; its body
  content (the alphabetised defined terms) takes the +1 penalty.
- **ANNEX III — ANNOUNCEMENT** is a real subdoc containing a joint
  press release that the parties agreed to issue. Because it is
  attached as an annex with a descriptive title, it is in scope.
  Its body content takes the +1 penalty.
- **The "ABOUT PANDORA" / "ABOUT KKR" / contact block is OUT of
  scope.** This is the canonical filing-trailer pattern: a
  press-release-style company description plus media-contact
  emails, appended by the SEC filer after the agreement signature
  and any attached annexes. There is no subdoc header preceding it,
  so the structural rule drops it. See
  [`scope_rule.md`](scope_rule.md) §"Trailer types seen in the
  corpus".
- **The trailer drop is positional, not phrase-based.** A press
  release that IS attached properly as a real subdoc (a
  hypothetical "EXHIBIT C — JOINT PRESS RELEASE") would stay in
  scope. The "ABOUT PANDORA" text is dropped not because it
  contains the word "ABOUT" but because it sits in the trailer
  position with no subdoc header in front of it.

## Depth distribution

Expected shape (approximate; ranges reflect doc2dict grouping
variation):

```
depth 0:    1 record    (title)
depth 1:  ~20 records   (preamble paragraphs, ARTICLE I–IX
                         headers, IN WITNESS WHEREOF, ANNEX I/II/III
                         headers, EXHIBIT A header)
depth 2:  ~70 records   (Sections inside main-body Articles,
                         signature-page lines, ARTICLE 1/2 inside
                         ANNEX II, content directly under each
                         subdoc header)
depth 3:  ~30 records   ((a)/(b) lettered items inside main-body
                         Sections; Section 1.1 / 2.1 inside ANNEX
                         II's ARTICLEs)
depth 4:   ~5 records   ((i)/(ii) roman items inside main-body
                         lettered (a)/(b); lettered (a) inside
                         ANNEX II's Sections)
depth 5+:  rare         (would require deep roman/letter nesting
                         inside ANNEX II's Sections)
```

The deeper depths inside ANNEX II are what makes this document a
useful counterpart to the Momenta/CSL example, whose subdocs were
redacted. Here the +1 penalty manifests as visible records.

## See also

- [`examples_with_subdocs.md`](examples_with_subdocs.md) — the
  synthetic case showing depths 4–7 under stacked subdoc nesting.
  Pandora/KKR exercises one level of subdoc nesting in real text.
- [`examples_with_subdocs_license.md`](examples_with_subdocs_license.md)
  — Momenta/CSL with redacted subdoc bodies; useful contrast to
  this fully-visible case.
- [`scope_rule.md`](scope_rule.md) — the structural test that
  drops the TABLE OF CONTENTS and the post-signature press-release
  trailer.
- [`level_rubric.md`](level_rubric.md) §"Worked depth chain" —
  the ARTICLE / Section / lettered nesting walkthrough.

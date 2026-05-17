# Scope rule — what is "the agreement" vs filing-context noise

## Document type is irrelevant — all agreements are in scope

The parser treats every agreement type the same way: private bilateral
contracts, **government contracts and amendments** (e.g. SF-30
modifications), international/cross-border agreements, **unilateral
instruments** (guaranties, options, releases, designations of agent,
irrevocable proxies), multilateral / tri-party agreements. The
structural patterns it relies on (signature lines, attached
subdocuments with descriptive titles, numbered/lettered clause
markers) are common across all of these. Do not add code paths that
branch on document class.

The only thing the scope rule filters out is **non-agreement
metadata**: filing context that travels with the SEC EX-10
attachment but isn't part of the agreement itself.

## Pre-title cover wedges

Anything that appears in document order BEFORE the agreement title
is, by definition, not a descendant of the title (see
[`level_rubric.md`](level_rubric.md) §"Title is the root of the
agreement") — so it cannot be part of the agreement. Examples:

  - SEC exhibit tag (`EXHIBIT 10.25`).
  - Registrant short name on its own line (`ULURU Inc.`) sitting
    above the title.
  - Filing date stamp (`Filed: March 4, 2017`).
  - `EX-10.25` / `EX-10_25` classifier line emitted by EDGAR tooling.
  - Page-numbered cover with the form type and CIK.

All of these are dropped from the JSONL automatically by the
title-as-root rule — no extra blocklist needed. They are retained
in the parquet with `scope="trailer"` so audit tooling can recover
them. The detection works because the parser locates the title
first, then walks the section list and emits only the title's
descendants. Sections that precede the title are siblings of the
title at most, never descendants, so they never reach the JSONL.

## Non-agreement metadata, structurally

SEC EDGAR filings frequently include filing-context content surrounding
the agreement: cover-page exhibit numbers, page-numbered TOCs, post-
signature press releases, "Forward Looking Statement" disclaimers,
"About [Company]" company descriptions, "Investor Contact:" / "Media
Contact:" blocks. None of these are the agreement; all of them inflate
parser output and contaminate regression baselines.

This rule is how the parser identifies which content is in scope for
the JSONL (and which is parquet-only audit material).

### Trailer types seen in the corpus

The following kinds of content commonly appear post-signature and
should land in the parquet with `scope="trailer"` (never in the
JSONL):

| trailer type                          | typical contents                                       |
|---------------------------------------|--------------------------------------------------------|
| Press release                         | "FOR IMMEDIATE RELEASE", date line, headline, body     |
| Forward-looking statement             | "This release contains forward-looking statements…"    |
| Company description ("About X")       | One-paragraph corporate boilerplate                    |
| Investor / media contact block        | Names, phone numbers, email addresses                  |
| Filing-page footer                    | Page numbers, exhibit-reference banners                |
| Re-stated SEC envelope                | Stray `EXHIBIT 10.x` tag re-appearing after signatures |
| Cover-page TOC                        | "TABLE OF CONTENTS" with page numbers (layout-only)    |
| Trailing exhibit reference list       | "Exhibits and Schedules:\n  Exhibit A — …\n  …" stub   |

None of these have a real-subdoc header in front of them, so they
fall outside scope under the rule below. If a press release IS
attached properly as `EXHIBIT B — FORM OF JOINT PRESS RELEASE`, it
stays in scope as a real subdoc — same words, different structural
position, opposite scope decision.

## The rule, mechanically

A section S (and all its descendants) is **OUT OF SCOPE** for JSONL
output if BOTH of the following hold:

  1. S appears AFTER the SIGNATURE BLOCK of the main agreement, AND
  2. S is NOT a descendant of a REAL SUBDOCUMENT.

Otherwise S is **IN SCOPE** (emit to JSONL as before).

Records emitted to the parquet always include all sections, with the
new `scope` column set to either `"agreement"` or `"trailer"` so audit
tooling can recover the dropped content.

## Detecting the SIGNATURE BLOCK

The signature block is the cluster of records at the end of the main
agreement showing who signed. Heuristic:

  - Find the LAST section in document order whose direct text contains
    a US-style signature line: `/s/\s+\w+` (e.g. `/s/ Jane Doe`,
    `/s/ Terrance K. Wallberg`).
  - The signature block ENDS at the last consecutive level-1 section
    that follows it AND remains structurally part of the signature
    block (subsequent `By:`/`Name:`/`Title:` blocks, `INDEMNITEE`
    label, second signatory's `/s/...`).
  - Heuristic for "still part of signature block": same depth, and
    span content matches `(?i)by:\s|name:\s|title:\s|address:\s|/s/\s`
    OR is a 1-3 word company-style label (e.g. `INDEMNITEE`).

The exact cutoff is the LAST section meeting that heuristic. After
that section ends, we are POST-SIGNATURE.

## Detecting a REAL SUBDOCUMENT

A real subdocument is a section S where ALL of the following hold:

  1. `S.cls in {"exhibit", "schedule", "appendix", "annex"}`.
  2. `S.is_envelope == False` (the SEC `<DOCUMENT>` envelope itself
     does not qualify; that's filing metadata, not a subdoc).
  3. `S.title` contains DESCRIPTIVE TEXT beyond the bare
     identifier — i.e. the title matches
     `^(EXHIBIT|SCHEDULE|APPENDIX|ANNEX)\s+[A-Z0-9.\-]+\s*[—\-:].+$`,
     where the part after the separator is at least one descriptive
     word ("Form of Notice", "Joint Press Release", "Definitions").

Examples:

| title                                              | real subdoc? |
|----------------------------------------------------|--------------|
| `EXHIBIT 10.25`                                    | NO (envelope or bare identifier) |
| `EXHIBIT A`                                        | NO (no descriptive text BUT must be appended to real title of the document, see ***) |
| `EXHIBIT B — FORM OF JOINT PRESS RELEASE`          | YES |
| `SCHEDULE 1: DEFINITIONS`                          | YES |
| `Appendix III - Transfer Procedures`               | YES |
| `ANNEX I`                                          | NO |
| `ANNEX I - Wire Instructions`                      | YES |

### Subdoc rescue — bare identifier followed by descriptive title

Some agreements split the subdoc identifier across two consecutive
sections: the bare exhibit tag on one line, the descriptive title on
the next. Neither line on its own passes the real-subdoc test — the
identifier lacks descriptive text, and the title line doesn't start
with one of the four subdoc keywords. Together they ARE a real
subdoc, and the parser rescues them as one.

The rescue rule:

  - A section whose title matches
    `^(EXHIBIT|SCHEDULE|APPENDIX|ANNEX)\s+[A-Z0-9.\-]+\s*$` (a bare
    identifier — no descriptive text), AND
  - is immediately followed by a section whose title is a
    descriptive heading (e.g. starts with `FORM OF`, `JOINT`,
    `STATEMENT OF`, or is otherwise a substantive title).

→ The two sections are treated together as a real-subdoc boundary.
Both lines emit to the JSONL at depth 1 (or deeper if nested); the
+1 subdoc penalty applies to everything that follows them inside
the rescued subdoc.

The example below illustrates the rescue with a placeholder
descriptive title — note the two L1 records and the body content
that descends from them.

```text
EXHIBIT A
FORM OF UGA UGA BUCETA

This buceta-note is valid for any brothel of the United States.
```
| title                                              | real subdoc? |
|----------------------------------------------------|--------------|
| `EXHIBIT A`                                        | NO (no descriptive text BUT must be appended to real title of the document, see ***) |
| `FORM OF UGA UGA BUCETA`                           | YES (title of the document) |

```jsonl
{"idx":0,"order":0,"level":0,"span":"INDEMNIFICATION AGREEMENT"}
{"idx":0,"order":1,"level":1,"span":"THIS INDEMNIFICATION AGREEMENT (the \"Agreement\") is made and entered into as of February 27, 2017 between Horny Brothels Inc., a Nevada corporation (the \"Company\"), and Bucetilda da Silva (\"Indemnitee\")."}
…
{"idx":0,"order":N+k-1,"level":1,"span":"EXHIBIT A"}
{"idx":0,"order":N+k,"level":1,"span":"FORM OF UGA UGA BUCETA"}


## Why this rule (and not phrase matching)

Earlier attempts blocked specific phrases (`MEDIA CONTACT:`, `Forward
Looking Statement`, `About <Company>`). The model gamed every check
by stripping the literal keyword and keeping the surrounding press-
release body. Phrase blocklists are unbounded and adversarial.

The structural rule cuts at the agreement's natural boundary —
signatures — and only re-admits content that is properly headered as
an attached subdocument. Press-release contact blocks, About-Company
boilerplate, forward-looking disclaimers, etc. don't have a real
subdoc header before them, so they fall outside scope automatically.

The rule does NOT cut all press-release content. A press release
attached as `EXHIBIT B — FORM OF JOINT PRESS RELEASE` has a real
subdoc header and stays. That's the legitimate-attachment case
Arthur wants preserved.

## Schema impact

`parse_doc2dict_with_config_nodes.parquet` gains one column:

  - `scope: str` — `"agreement"` for in-scope sections, `"trailer"`
    for sections dropped from JSONL because they're filing context.

`parse_doc2dict_with_config_nodes.jsonl` is unchanged in shape (still
3 keys: `idx`, `level`, `span`); it just has fewer records when the
document has a filing trailer.

## Forbidden approaches

The following are NOT allowed in the parser implementation:

- Hardcoded lists of phrases to filter (`["press release", "Forward
  Looking Statement", "About Momenta", ...]`).
- `if "press release" in span:` content-based blocklists.
- Regex matching against filing-trailer keywords IN PLACE OF the
  structural rule.
- Capping levels (clamp `depth` to 7, etc.) to bypass validator
  checks while leaving content in the JSONL.
- Document-class-specific code paths (`if is_government_contract:`,
  `if is_unilateral:`). The scope rule operates on structure, not on
  what category of agreement the document is.

The validator scans the parser source for these patterns and rejects
the freeze if found. See [`freeze_command.md`](freeze_command.md)
for what the validator looks at.

## Worked walkthrough — one idx, classified end to end

Consider this minimal idx, with each source section annotated by
its `cls` and the structural reason for its scope decision:

```text
EXHIBIT 10.25                                  ← cls=exhibit, is_envelope=True
ULURU Inc.                                     ← pre-title cover line (precedes title)
INDEMNIFICATION AGREEMENT                      ← cls=title
THIS INDEMNIFICATION AGREEMENT… (preamble)     ← cls=preamble
WITNESSETH THAT: WHEREAS…                      ← cls=recital
1. Indemnity of Indemnitee.                    ← cls=section
  (a) Proceedings Other Than…                  ← cls=section (lettered child)
IN WITNESS WHEREOF, the parties…               ← cls=signature (operating clause)
ULURU Inc.                                     ← signature-block line (party label)
By: /s/ Terrance K. Wallberg                   ← signature-block line
INDEMNITEE                                     ← signature-block line (role label)
/s/ Vaidehi Shah                               ← signature-block line
Vaidehi Shah                                   ← signature-block line
EXHIBIT A — FORM OF NOTICE                     ← real subdoc header
1. Scope of Notice.                            ← subdoc body
FOR IMMEDIATE RELEASE                          ← trailer (press release)
ULURU Inc. announces…                          ← trailer (press-release body)
About ULURU                                    ← trailer (company description)
Investor Contact: ir@uluru.com                 ← trailer (contact block)
```

Classification:

| section                              | scope        | reason                                   | in JSONL? |
|--------------------------------------|--------------|------------------------------------------|-----------|
| EXHIBIT 10.25                        | (envelope)   | `is_envelope=True`, precedes title       | NO        |
| ULURU Inc. (pre-title)               | trailer      | precedes title in document order         | NO        |
| INDEMNIFICATION AGREEMENT            | agreement    | the title itself                         | YES (L0)  |
| THIS INDEMNIFICATION AGREEMENT…      | agreement    | direct child of title                    | YES (L1)  |
| WITNESSETH THAT: WHEREAS…            | agreement    | direct child of title                    | YES (L1)  |
| 1. Indemnity of Indemnitee.          | agreement    | direct child of title                    | YES (L1)  |
| (a) Proceedings Other Than…          | agreement    | child of "1. Indemnity"                  | YES (L2)  |
| IN WITNESS WHEREOF…                  | agreement    | signature-page operating clause          | YES (L1)  |
| ULURU Inc. (signatory label)         | agreement    | signature-block line                     | YES (L2)  |
| By: /s/ Terrance K. Wallberg         | agreement    | signature-block line                     | YES (L2)  |
| INDEMNITEE                           | agreement    | signature-block line                     | YES (L2)  |
| /s/ Vaidehi Shah                     | agreement    | signature-block line                     | YES (L2)  |
| Vaidehi Shah                         | agreement    | signature-block line                     | YES (L2)  |
| EXHIBIT A — FORM OF NOTICE           | agreement    | real subdoc (passes detection test)      | YES (L1)  |
| 1. Scope of Notice.                  | agreement    | subdoc body; +1 subdoc penalty           | YES (L2)  |
| FOR IMMEDIATE RELEASE                | trailer      | post-signature, no subdoc header above   | NO        |
| ULURU Inc. announces…                | trailer      | post-signature, no subdoc header above   | NO        |
| About ULURU                          | trailer      | post-signature, no subdoc header above   | NO        |
| Investor Contact: ir@uluru.com       | trailer      | post-signature, no subdoc header above   | NO        |

Notice the same literal text `ULURU Inc.` appears three times in
the source and gets three different classifications: dropped as
pre-title cover, kept as signature-block party label, dropped as
press-release header. The decision is purely positional — phrase
matching would not get this right.

## See also

- [`level_rubric.md`](level_rubric.md) — the depth contract that
  makes "descendant of the title" a precise structural concept.
- [`examples_main_agreement.md`](examples_main_agreement.md) — the
  worked main-agreement case showing signature-block lines kept at
  depth 2.
- [`examples_with_subdocs.md`](examples_with_subdocs.md) — the
  +1 penalty applied to subdoc descendants, including the rescue
  case.
- [`freeze_command.md`](freeze_command.md) — the validator that
  scans the parser source for the forbidden approaches listed
  above and enforces the reconstruction gate.

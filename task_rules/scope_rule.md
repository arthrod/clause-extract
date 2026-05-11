# Scope rule — what is "the agreement" vs filing-context noise

## Document type is irrelevant; "agreement" is the only criterion

The parser handles **all kinds of agreements** with the same rules:

  - Private bilateral contracts (credit, license, supply, services).
  - Public and regulated agreements: **government contracts and
    amendments** (e.g. SF-30 contract modifications), tariffs.
  - International / cross-border agreements.
  - **Unilateral agreements**: guaranties, options, releases,
    designations of agent, irrevocable proxies, waivers — instruments
    where only one party makes a binding promise.
  - Multilateral / tri-party agreements.

The scope rule does **not** filter by what kind of agreement the
document is. Whatever sits inside an EX-10 attachment as the
substantive agreement gets parsed. The rule below is only about
separating the agreement (which goes into the JSONL) from
**non-agreement metadata** that travels with the SEC filing
(envelope, post-signature press release, About-Company paragraphs,
forward-looking-statement disclaimers, contact blocks).

## Non-agreement metadata, structurally

SEC EDGAR filings frequently include filing-context content surrounding
the agreement: cover-page exhibit numbers, page-numbered TOCs, post-
signature press releases, "Forward Looking Statement" disclaimers,
"About <Company>" company descriptions, "Investor Contact:" / "Media
Contact:" blocks. None of these are the agreement; all of them inflate
parser output and contaminate regression baselines.

This rule is how the parser identifies which content is in scope for
the JSONL (and which is parquet-only audit material).

## The rule, mechanically

A section S (and all its descendants) is **OUT OF SCOPE** for JSONL
output if BOTH of the following hold:

  1. S appears AFTER the SIGNATURE BLOCK of the main agreement, AND
  2. S is NOT a descendant of a REAL SUBDOCUMENT.

Otherwise S is **IN SCOPE** (emit to JSONL as before).

Records emitted to the parquet always include all sections, with the
`scope` column set to either `"agreement"` or `"trailer"` so audit
tooling can recover the dropped content.

## Detecting the SIGNATURE BLOCK

The signature block is the cluster of records at the end of the main
agreement showing who signed. The detection is purely structural —
it does not look at company names or party identities, so it works
for private, government, unilateral, and international agreements
identically:

  - Find the LAST section in document order whose direct text contains
    a US-style signature line: `/s/\s+\w+` (e.g. `/s/ Jane Doe`,
    `/s/ Terrance K. Wallberg`, `/s/ Eric Trachtenberg`).
  - The signature block ENDS at the last consecutive level-1 section
    that follows it AND remains structurally part of the signature
    block (subsequent `By:`/`Name:`/`Title:` blocks, role label,
    second signatory's `/s/...`).
  - Heuristic for "still part of signature block": same depth, and
    span content matches `(?i)by:\s|name:\s|title:\s|address:\s|/s/\s`
    OR is a 1–3 word role/company-style label (e.g. `INDEMNITEE`,
    `CONTRACTOR/OFFEROR`, `UNITED STATES OF AMERICA`).

The exact cutoff is the LAST section meeting that heuristic. After
that section ends, we are POST-SIGNATURE.

**Unilateral agreements** have only one signatory but still have a
signature block by the same rule (one `/s/...` line and its
surrounding name/title blocks). A unilateral instrument with no
signature line at all is degenerate; treat the whole document as
in-scope.

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
| `EXHIBIT A`                                        | NO (no descriptive text) |
| `EXHIBIT B — FORM OF JOINT PRESS RELEASE`          | YES |
| `SCHEDULE 1: DEFINITIONS`                          | YES |
| `Appendix III - Transfer Procedures`               | YES |
| `ANNEX I`                                          | NO |
| `ANNEX I - Wire Instructions`                      | YES |

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

## Why this rule works across document types

The rule references three structural primitives — section depth, a
`/s/...` signature line, and section `cls`. None of those depend on
the document being a private contract, a government form, or a
unilateral instrument. The same code paths produce correct in-scope
/ out-of-scope tagging for all of them:

  - A government SF-30 amendment has `/s/ <contractor>` and
    `BY <contracting officer>` lines as its signature block;
    everything after is non-agreement metadata (FAR clauses
    referenced, page footers) and gets `scope="trailer"`.
  - A unilateral guaranty has one `/s/...` line; the small block
    after (date, witness) is part of the signature; subsequent
    "About <Company>" or contact blocks are post-signature trailer.
  - A bilateral credit agreement has the conventional two signature
    sub-blocks plus attached exhibits/schedules with descriptive
    titles; signatures end the agreement; subdocs continue in scope.

Document type is just shape; the scope rule operates on shape.

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
  `if is_unilateral:`). The structural rule applies the same to all
  types; if you need a branch by document class, the rule needs
  fixing instead.

The validator scans the parser source for these patterns and rejects
the freeze if found. See [`freeze_command.md`](freeze_command.md)
for what the validator looks at.

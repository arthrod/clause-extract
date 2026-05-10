# Canonical record schema

This document specifies the canonical clause-record schema produced by `clause-extract`. Every consumer of the canonical dataset can rely on these field names, types, and derivation rules.

The schema is **locked** as of repo creation. Changes require a PR with the rationale documented in `docs/DECISIONS.md`.

> **Where this fits relative to the immediate goal.** The parser scripts (`scripts/parse_*.py`) currently emit a minimal **3-key JSONL** — `{idx, level, span}` — directly from each parser's output. That's the inspection-and-measurement format used to verify the immediate parsing goal (concat-of-spans ≈ source document). The canonical schema described below is the **destination format** built on top of parser output once each parser meets the reconstruction-quality bar. Until that bar is cleared, the JSONL is what gets used; the canonical records described here are the next layer up.

---

## Top-level layout

The output dataset (`arthrod/clause-prob-canonical`, HF private) has **one row per source document** with this layout:

| Column | Type | Description |
| --- | --- | --- |
| `doc_id` | string | Stable identifier, format `<corpus_repo_short>#<corpus_idx>`. E.g. `m22017#4`. |
| `corpus_idx` | int | Source row index in the corpus parquet (0..N-1). |
| `submission` | struct | Pass-through of the corpus's `submission` column (cik, company, form_type, etc.). |
| `document_metadata` | struct | Pass-through of the corpus's `document_metadata` column. |
| `parse_status` | enum | `"ok" \| "image_only" \| "no_html_block" \| "parser_error"`. |
| `parse_quality` | enum | `"clean" \| "uncertain" \| "excluded"`. See bounds below. |
| `is_deep_taxonomy_doc` | bool | True if structural depth legitimately exceeds 4 (FAR clauses, multi-jurisdiction grants, airport rate methodologies). See `docs/DECISIONS.md` §depth-cap. |
| `n_clauses` | int | Number of canonical records in `clauses`. |
| `clauses` | list[struct] | Canonical clause records (the full ledger for this document). See "Clause record" below. |
| `audit` | struct | Document-level audit fields. See "Document-level audit" below. |
| `created_at` | timestamp | UTC ISO-8601. |

---

## Clause record

Each entry in the `clauses` list is a struct with these fields:

| Field | Type | Description |
| --- | --- | --- |
| `canonical_path` | list[string] | **Primary identifier.** List of segments forming the hierarchical address. Example: `["section:5", "subsection:a", "sub_subsection:v"]` for clause `5(a)(v)`. The list form is what queries traverse. |
| `clause_id` | string | **Joinable string serialization** of `canonical_path`. Round-trips with `canonical_path` via `canonical_id_parser.parse_clause_id` / `_segments_to_clause_id`. Examples: `"5(a)(v)"`, `"exhibit10.26"`, `"la.1.1"`, `"recital_A"`, `"def(a)"`, `"IV.2.iii"`. |
| `kind` | enum | Primary kind label of the leaf segment. See "Kind taxonomy" below. |
| `id` | string | The leaf segment's identifier portion (e.g., `"v"` for `5(a)(v)`, `"10.26"` for `exhibit10.26`). |
| `structural_depth` | int | **Computed from `len(canonical_path) - 1`.** NOT from parser depth. See `docs/DECISIONS.md` §why-id-string-depth. |
| `parent_canonical_path` | list[string] \| null | The ancestor's `canonical_path`, or `null` for root-level. Computed by dropping the last segment. Special cases (subdocument-only parents) covered below. |
| `child_canonical_paths` | list[list[string]] | Direct children's `canonical_path` lists. Populated during canonicalization for fast traversal. |
| `header_text` | string | Verbatim header line from source (smart quotes, em-dashes, etc. preserved as Unicode). Empty string `""` if the clause is header-only with no separate header line. |
| `body_text` | string | **Full body text including all descendants' text.** Redundancy with children is intentional (per Arthur's decision) — queries via `canonical_path` filter to specific levels; downstream consumers need the parent block sometimes. Empty string `""` for header-only clauses (no body in source). |
| `is_keyword_prefixed` | bool | True if the source header contained an explicit keyword (`Section`, `Article`, `Exhibit`, etc.) — useful for distinguishing `1(a)` (bare numeric) from `section1(a)` (keyword-prefixed). |
| `is_synthetic` | bool | True if this record was synthesized by the post-processor (subdocument scope record, no source line corresponds). False for records derived from actual headers. |
| `subdocument_class` | enum \| null | Set only when `kind == "subdocument"`. Three-value enum (see "Subdocument class" below). Null otherwise. |
| `dd_node_ids` | list[string] | Node IDs in the `doc2dict` parse output that contributed to this clause. List (not single value) because dd may split the ID-line and body across nodes. |
| `lx_span_ids` | list[string] | Span IDs in the `lexnlp` regex section output that contributed. List because lex may shred inline enumerations into multiple spans. |
| `audit` | struct | Per-clause audit fields. See "Per-clause audit" below. |

---

## Kind taxonomy

The `kind` field uses this closed enum:

| Value | When to use |
| --- | --- |
| `exhibit` | Top-level "Exhibit X.Y" envelope. |
| `article` | "Article N" or "Article I/II/III". |
| `section` | "Section N" or bare numeric leaf at level 1 (`5`, `13`). |
| `subsection` | First-paren child (`(a)`, `(1)`). |
| `sub_subsection` | Second-paren child (`(a)(v)`). |
| `sub_sub_subsection` | Third-paren child. |
| `schedule` | "Schedule X". |
| `appendix` | "Appendix X". |
| `annex` | "Annex X". |
| `part` | Mid-document "Part N". |
| `title` | Mid-document "Title N". |
| `subtitle` | Sub-divisional "Subtitle X". |
| `recital` | `recital_A`, `recital.A` style. |
| `whereas` | `whereas.1` style. |
| `topical` | Named-topical roots (`preamble`, `closing`, `vesting`, `taxes`, `effective_date`) — clauses identified only by topic name without a numeric ID. |
| `subdocument` | A logical subdocument scope (named-in-source `Attachment I`, `Form of Promissory Note`, `Power Purchase Agreement` summary, etc.) materialized as a record. Always synthetic. |
| `introduction` | Document-introductory block (the preamble paragraphs before any structural marker). |

The decoupling of `kind` from `structural_depth` is deliberate. The same regex match (`(a)`) can appear at depth 1 (under a section) or depth 2 (under a subsection); the kind tracks paren-nesting position while `structural_depth` is the integer.

## Subdocument class

When `kind == "subdocument"`, `subdocument_class` is set to one of:

| Value | Meaning |
| --- | --- |
| `category_1_named_in_source` | The subdocument is explicitly named in the source text (e.g., "ATTACHMENT I — SUMMARY OF SPECIAL SEVERANCE BENEFITS", "FORM OF PROMISSORY NOTE", "POWER PURCHASE AGREEMENT" appearing as a section title). The synthetic record's `header_text` quotes that source line. |
| `category_2_extractor_label` | The prefix is an extractor-side scope label that doesn't correspond to a named subdocument in source (e.g., `def`, `cs`, `ac`, `notice`). The synthetic record's `header_text` is empty. |
| `auto_detected` | The detector v1 flagged this prefix during canonicalization without explicit human extraction confirmation. |

This is **metadata, not classification**. Downstream consumers can filter by `subdocument_class` to focus on real named subdocuments or treat all uniformly.

## Recognized canonical-path forms

The `canonical_id_parser` validates 100% on the 25-doc SOT (973 clauses). The recognized forms:

| Form | Examples | Encoding in `canonical_path` |
| --- | --- | --- |
| Envelope keyword | `exhibit10.26`, `schedule.A`, `article5` | `["exhibit:10.26"]`, `["schedule:A"]`, `["article:5"]` |
| Bare numeric + paren | `1`, `13(a)`, `13(a)(v)` | `["section:1"]`, `["section:13", "subsection:a"]`, `["section:13", "subsection:a", "sub_subsection:v"]` |
| Roman primary | `I`, `II`, `IX(a)`, `IV.2.iii` | `["article_roman:I"]`, `["article_roman:IV", "section:2", "section_roman:iii"]` |
| Section-prefixed | `section1`, `section2(b)(1)`, `section8(3)` | `["section@:1"]` (the `@` annotation preserves the keyword for round-trip) |
| Document-namespace | `la.1.1`, `cl.7.4`, `att1.epirp`, `ppa.term_duration`, `msw_concession.parties` | `["subdocument:la", "section:1.1"]`, `["subdocument:att1", "topical:epirp"]` |
| Topical roots | `preamble`, `closing`, `vesting`, `taxes`, `effective_date` | `["topical:preamble"]` |
| Recital encodings | `recital_A`, `recital.A`, `whereas.1` | `["recital@_:A"]`, `["whereas@.:1"]` |
| Definition entries | `def(a)`, `def(b)`...`def(j)` | `["subdocument_nodot:def", "subsection:a"]` |
| Roman with topical tail | `XI_dispute`, `XI_misc` | `["article_roman:XI", "topical_under:dispute"]` |
| Dot-letter tail | `3.01(f).A` | `["section:3.01", "subsection:f", "sub_subsection_dotted:A"]` |

The list form uses `kind:identifier` strings as a stable serialization. The string form (`clause_id`) is preferred as the public-facing identifier; the list is what the post-processor uses internally for path manipulation.

## Parent derivation rule

The default rule is **drop the last segment**.

```python
parent_canonical_path = canonical_path[:-1] if len(canonical_path) > 1 else None
```

Two special cases override this:

1. **Subdocument-only parent** → `null`. If dropping the last segment yields a single-segment path whose kind is `subdocument` or `subdocument_nodot`, the parent is `null` (root). Subdocuments are scopes, but their direct children's `parent_canonical_path` resolves to the synthetic subdocument record (which IS materialized) when one exists. This is reconciled at canonicalization time — see the post-processor spec.

2. **Roman-only ancestor of a topical-under leaf** → `null`. `XI_dispute`'s "parent" `XI` doesn't exist as a clause record in the source — it's a roman scope label. The leaf is treated as root.

## Bounds on `structural_depth`

Empirically, real EX-10 agreements cap at depth 4 (Article → Section → Subsection → letter-paren). The corpus full-scan showed 82% of docs at max_depth ≤ 4. Specialized documents (FAR contracts, multi-jurisdiction grants, airport rate methodologies) legitimately reach depth 7.

Canonicalization assigns:

| `structural_depth` of leaf | `parse_quality` outcome |
| --- | --- |
| `≤ 4` | `"clean"`. |
| `5..7` AND `is_deep_taxonomy_doc` evidence in source | `"clean"` with `is_deep_taxonomy_doc=true`. |
| `5..7` without deep-taxonomy evidence | `"uncertain"` (record is kept but flagged). |
| `> 7` | `"excluded"` (record is kept for audit but the document is excluded from aggregate clause-presence stats). |

`is_deep_taxonomy_doc` is set by checking whether the source document matches one of the validated deep-doc patterns from the categorization run (see `docs/DECISIONS.md` §depth-cap).

## Document-level audit

The top-level `audit` struct on each document row contains:

| Field | Type | Description |
| --- | --- | --- |
| `dd_n_nodes` | int | Number of nodes the doc2dict parse produced. |
| `dd_max_depth` | int | doc2dict's reported max depth. |
| `lx_n_spans` | int | Number of spans the lexnlp regex parse produced. |
| `lx_n_titled` | int | Number of lex spans with a parsed title. |
| `phantom_collapsed_node_ids` | list[string] | dd nodes identified as phantom parents (signature blocks, address lines, page rules) and collapsed during canonicalization. |
| `lx_corrected_abs_level_count` | int | Number of lex spans whose `abs_level` we overrode based on document context. |
| `lx_folded_span_ids` | list[string] | Lex spans that were folded into a parent clause (inline enumerations under a single header). |
| `body_reconciliation_score` | float \| null | RapidFuzz WRatio comparison of dd-derived body vs lex-derived body for this doc, in `[0, 100]`. Null if either parser produced no body. |
| `subdoc_detector_verdict` | enum | `"no_subdoc" \| "single_envelope" \| "multi_subdoc_v1" \| "ambiguous"`. |
| `subdoc_n_synthesized` | int | Number of synthetic subdocument records added. |
| `is_deep_taxonomy_doc` | bool | (Mirror of top-level for join convenience.) |
| `parse_status` | enum | (Mirror of top-level.) |
| `parse_quality` | enum | (Mirror of top-level.) |

## Per-clause audit

The `audit` struct on each clause record contains:

| Field | Type | Description |
| --- | --- | --- |
| `dd_tree_depth` | int \| null | doc2dict's reported depth for the contributing node, if exactly one. Null if 0 or 2+ nodes. |
| `dd_tree_depth_disagrees_with_id_depth` | bool | True iff `dd_tree_depth != structural_depth + offset`. The 22% of multi-level docs where dd tracks correctly will have this False; the 78% will have it True. Used as an audit signal, not as authority. |
| `lx_level` | int \| null | Lexnlp's `level` for the contributing span, if exactly one. |
| `lx_abs_level` | int \| null | Lexnlp's `abs_level` for the contributing span, if exactly one. |
| `extraction_notes` | string | Free-text notes about anomalies, adjudication decisions, or rules applied. |
| `is_reading_b_excluded_neighbor` | bool | True if this clause was originally a Reading B exclusion candidate (WHEREAS, WITNESSETH, NOW THEREFORE) but was retained because it carries an explicit identifier. False otherwise. |

## What this schema is NOT

This schema is a **structured representation of parsed clause content with hierarchy**. It is not:

- **A clause-type label.** No fields like `clause_type: "indemnification"`. Each record carries the source `header_text`, `body_text`, and structural position; categorizing those clauses by topic is a separate concern outside this project.
- **A retrieval index.** No embeddings, no vector fields. The output is structured text, not a search artifact.
- **An aggregated probability table.** Statistics like `P(clause_type | agreement_type)` are computed by downstream queries against this dataset, not stored in the records.

The deliverable is the parsed structure. Anything built on top of that — categorization, retrieval, statistics — is outside scope and outside this repo.

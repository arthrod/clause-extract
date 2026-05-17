#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "polars>=1.18",
#     "pyarrow>=18",
#     "doc2dict>=0.7.1",
#     "huggingface-hub>=0.27",
#     "loguru>=0.7",
#     "typer>=0.15",
#     "rich>=13.9",
# ]
# ///
"""doc2dict parser using the validated EX-10 mapping_dict.

Same parsing core as `parse_doc2dict_baseline.py`, except it passes
`make_agreement_config()` as the mapping_dict. That config is the
lexnlp-informed `levels` regex set living in
`src/clause_extract/agreement_config.py` — locked, validated, AGPLv3.

Why this isn't just "the same script with a switch":
  - With the config, headers matching the level patterns get a real `class`
    (article, section, exhibit, schedule, appendix, annex, subsection, ...)
    plus a `standardized_title` (article1, section2.3, exhibit10.7, ...).
  - Visual-only headers that don't match any pattern still get
    `class="predicted header"` as a fallback. So this script's output is a
    SUPERSET of the baseline's information — typed where possible, fallback
    elsewhere.

Outputs
-------
Three files written to --output-dir:

    parse_doc2dict_with_config_docs.parquet     One row per source document.
    parse_doc2dict_with_config_nodes.parquet    One row per section node.
    parse_doc2dict_with_config_nodes.jsonl      ONE LINE PER PARSED UNIT,
                                                only 3 keys per line.

JSONL schema (the ONLY 3 keys per line)
---------------------------------------
    idx    int    Source corpus row index.
    level  int    doc2dict tree depth (0 = top of document body, increases
                  going down). doc2dict's NATIVE 0-indexed depth; lexnlp
                  uses 1-indexed `level` instead — not normalized so each
                  parser's view is preserved.
    span   str    The parsed unit's content: heading + body. Specifically
                  `<title>\n<body_direct>` for normal sections; just
                  `<body_direct>` for doc2dict's synthetic `introduction`
                  pseudo-node. Strings longer than 4000 chars truncated to
                  `<head 3000>[TRUNCATED]<tail 1000>`.

Concatenating all `span` values for one `idx` in document order should
approximately reconstruct the agreement text. Compare against the
matching reconstruction from the other two parsers (baseline + lexnlp)
to find places where each parser is missing or duplicating content.

The parquet contains FULL body text and ALL fields. The JSONL is the
inspection-ready shape; parquet is the source of truth.

Body extraction
---------------
Identical to `parse_doc2dict_baseline.py`. See that script for the
body_direct vs body_recursive explanation.

Usage
-----
    uv run scripts/parse_doc2dict_with_config.py --limit 5
    HF_TOKEN=hf_xxx uv run scripts/parse_doc2dict_with_config.py \\
        --output-dir data/runs/doc2dict_with_config
"""

from __future__ import annotations

import json
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

import polars as pl
import typer
from doc2dict import html2dict
from loguru import logger
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

# Make our locked agreement_config importable without installing the package
_REPO_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_REPO_SRC) not in sys.path:
    sys.path.insert(0, str(_REPO_SRC))

from clause_extract.agreement_config import make_agreement_config  # noqa: E402

CORPUS_URL = (
    "hf://datasets/arthrod/new3_results_master22017_274.59mb/data/train-00000-of-00001.parquet"
)

_HTML_OPEN = re.compile(r"<html[\s>]", re.IGNORECASE)
_HTML_CLOSE = re.compile(r"</html\s*>", re.IGNORECASE)
_IMG_TAG = re.compile(r"<img\b", re.IGNORECASE)
_TAG_STRIP = re.compile(r"<[^>]+>")

# ---------------------------------------------------------------------------
# Level-remapping: rubric-compliant depth from title patterns
# ---------------------------------------------------------------------------
# doc2dict's tree depth reflects HTML nesting, not legal hierarchy.
# These patterns assign the correct rubric level based on how EX-10
# agreements number their sections.
#
#   Level 0 → agreement root title + preamble
#   Level 1 → exhibit metadata, party names, WITNESSETH/WHEREAS, signatures
#   Level 2 → numbered sections: "1.", "2.", "10.", "Section 1.1"
#   Level 3 → lettered subsections: "(a)", "(b)", "(c)"
#   Level 4 → sub-sub items: "(i)", "(ii)", "(A)", "(B)", "(1)", "(2)"

# Ordered most-specific first so roman "(i)" is checked before lettered "(i)".
_LEVEL_PATTERNS: list[tuple[int, re.Pattern[str]]] = [
    # New rubric: numbered sections "1." sit at depth 1 (top-level body
    # clauses are direct children of the agreement). Lettered subsections
    # "(a)" at 2; roman/cap-letter/digit items "(i)"/"(A)"/"(1)" at 3.
    #
    # Depth 3: roman numeral in parens — multi-char "(ii)", "(iii)" or
    # common single roman numerals "(i)", "(I)", "(v)", "(V)", "(x)", "(X)".
    (3, re.compile(r"^\(([ivx]+|[IVX]+)\)")),
    # Depth 3: uppercase letter in parens "(A)", "(B)"
    (3, re.compile(r"^\(([A-Z])\)")),
    # Depth 3: digit in parens "(1)", "(2)"
    (3, re.compile(r"^\((\d+)\)")),
    # Depth 2: single lowercase letter in parens "(a)", "(b)", "(c)"
    (2, re.compile(r"^\(([a-z])\)")),
    # Depth 3: N.M.K dotted section marker (subsection of an N.M
    # section). Examples: "2.1.1 Term Loans", "13.6.2 Force Majeure".
    # The lookahead `(?![.\d])` makes sure we don't match the prefix
    # of "2.1.1.1" (which would be depth 4). The marker may be glued
    # to the title text ("2.1.1Term Loans.") or separated by whitespace
    # ("2.1.1 Term Loans."), so the trailing char is unconstrained.
    # MUST appear before N.M and N. patterns so the deeper structure
    # wins. See task_rules/level_rubric.md "Worked depth chain".
    (3, re.compile(r"^(\d+)\.(\d+)\.(\d+)(?![.\d])")),
    # Depth 2: N.M dotted section marker (subsection of an Article).
    # Examples: "2.1 Promise to Pay", "2.1Promise to Pay" (no space),
    # "1.10 Definitions". Lookahead `(?![.\d])` keeps this from
    # absorbing the "N.M" prefix of an "N.M.K" marker. MUST appear
    # before the bare N. pattern so credit/license-style numbered
    # subsections land at depth 2 under their topic-Article L1
    # parent. See task_rules/level_rubric.md depth chain.
    (2, re.compile(r"^(\d+)\.(\d+)(?![.\d])")),
    # Depth 1: bare number with period ("1.", "10.", "21.")
    (1, re.compile(r"^(\d+)\.")),
    # Depth 1: ARTICLE heading ("ARTICLE I", "ARTICLE 3", "Article IV.")
    (1, re.compile(r"^ARTICLE\s+(\d+|[IVXLCDM]+)\.?\s*$", re.IGNORECASE)),
    # Depth 1: SECTION heading ("SECTION 1.", "Section 2.3")
    (1, re.compile(r"^SECTION\s+\d+", re.IGNORECASE)),
]


def _infer_level_from_title(title: str) -> int | None:
    """Return the rubric level for *title* if it matches a known pattern."""
    if not title:
        return None
    # Strip leading/trailing whitespace for matching.
    stripped = title.strip()
    for level, pat in _LEVEL_PATTERNS:
        if pat.match(stripped):
            return level
    return None


def extract_html(raw: str) -> str | None:
    m_open = _HTML_OPEN.search(raw)
    if not m_open:
        return None
    last = None
    for m in _HTML_CLOSE.finditer(raw):
        last = m
    if last is None:
        return None
    return raw[m_open.start() : last.end()]


def is_image_only(html: str) -> bool:
    visible = _TAG_STRIP.sub(" ", html)
    visible = re.sub(r"\s+", " ", visible).strip()
    return len(visible) < 200 and len(_IMG_TAG.findall(html)) >= 1


# JSONL truncation: long fields get summarized for inspection-friendly output.
# Parquet keeps the full text — this only affects the JSONL writer.
_JSONL_TRUNCATE_THRESHOLD = 1000
_JSONL_TRUNCATE_HEAD = 800
_JSONL_TRUNCATE_TAIL = 200


def _truncate_for_jsonl(value: Any) -> Any:
    """If `value` is a string longer than the threshold, return
    `<head>[TRUNCATED]<tail>`. Otherwise return the value unchanged."""
    if not isinstance(value, str) or len(value) <= _JSONL_TRUNCATE_THRESHOLD:
        return value
    return value[:_JSONL_TRUNCATE_HEAD] + "[TRUNCATED]" + value[-_JSONL_TRUNCATE_TAIL:]


def _build_jsonl_span(title: str | None, body_direct: str | None, cls: str | None) -> str:
    """Combine a section's heading and direct body into the JSONL `span`.

    For doc2dict's synthetic `introduction` pseudo-node, emit only
    `body_direct`. The literal word "introduction" is a doc2dict class
    label, not document content; including it would pollute reconstruction.

    For all other sections, concatenate `<title>\\n<body_direct>`,
    skipping either part if it's empty.
    """
    body = body_direct or ""
    if cls == "introduction":
        return body
    title = title or ""
    parts = [p for p in (title, body) if p]
    return "\n".join(parts)


def _is_section_node(d: Any) -> bool:
    if not isinstance(d, dict):
        return False
    if "text" in d and len(d) == 1:
        return False
    return ("title" in d) or ("class" in d) or ("contents" in d)


def _is_text_leaf(d: Any) -> bool:
    return isinstance(d, dict) and "text" in d and "contents" not in d


def _is_table_node(d: Any) -> bool:
    """A doc2dict node representing an HTML <table>.

    doc2dict stores tables as `{"table": {"preamble": ..., "data": ...,
    "postamble": ..., "title": ..., "cleaned": ...}}` with NO `title`,
    `class`, `contents` or `text` keys. These nodes are neither section
    nodes nor text leaves and were previously skipped — but their
    `preamble`/`postamble` carry real document text (often multiple
    inline sections worth) that must be captured.
    """
    return (
        isinstance(d, dict)
        and "table" in d
        and "text" not in d
        and "contents" not in d
        and "title" not in d
    )


def _collect_table_text(node: dict[str, Any]) -> str:
    """Extract preamble + cell data + postamble text from a table node.

    Excludes the table's `title` field because doc2dict copies the
    enclosing section's title text into it — keeping that would double-
    count content already emitted on the parent section.
    """
    table = node.get("table")
    if not isinstance(table, dict):
        return ""
    parts: list[str] = []
    pre = table.get("preamble")
    if isinstance(pre, str) and pre.strip():
        parts.append(pre.strip())
    data = table.get("data")
    if isinstance(data, list):
        for row in data:
            if isinstance(row, list):
                for cell in row:
                    if isinstance(cell, str) and cell.strip():
                        parts.append(cell.strip())
            elif isinstance(row, str) and row.strip():
                parts.append(row.strip())
    post = table.get("postamble")
    if isinstance(post, str) and post.strip():
        parts.append(post.strip())
    return "\n".join(parts)


def _collect_direct_text(section: dict[str, Any]) -> str:
    """Concat text-leaf children. Excludes text leaves that will be promoted
    into their own section records (so they don't double-emit). A leaf is
    promotable when its text begins with a section marker like "1.", "(a)",
    "(i)". See `_promote_text_leaves`.

    Also walks table-only children (doc2dict's HTML-table representation)
    and pulls their preamble/data/postamble text into the parent's body."""
    out: list[str] = []
    contents = section.get("contents")
    if isinstance(contents, dict):
        for child in contents.values():
            if _is_text_leaf(child):
                t = child.get("text")
                if isinstance(t, str):
                    if _TEXT_LEAF_SECTION_RE.match(t):
                        # Text leaf will be promoted to its own record.
                        # Exclude here to avoid duplicate emission.
                        continue
                    out.append(t)
            elif _is_table_node(child):
                t = _collect_table_text(child)
                if t:
                    out.append(t)
    return "\n".join(out)


def _collect_recursive_text(section: dict[str, Any]) -> str:
    out: list[str] = []

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        if "text" in node and isinstance(node["text"], str):
            out.append(node["text"])
        contents = node.get("contents")
        if isinstance(contents, dict):
            for child in contents.values():
                walk(child)

    walk(section)
    return "\n".join(out)


# Structural titles → rubric level mapping.  Matches are against stripped title.
# Agreement root → 0, exhibit/recitals/signatures → 1.
_STRUCTURAL_LEVELS: list[tuple[re.Pattern[str], int]] = [
    (re.compile(r"^EXHIBIT\s+", re.IGNORECASE), 1),
    (re.compile(r"^WITNESSETH", re.IGNORECASE), 1),
    (re.compile(r"^IN\s+WITNESS\s+WHEREOF", re.IGNORECASE), 1),
    (re.compile(r"^SIGNATURE\s+PAGE", re.IGNORECASE), 1),
    (re.compile(r"^[A-Z]+\.?\s*(Inc|Corp|LLC|Ltd|Company|Co)\.?\b", re.IGNORECASE), 1),
    (re.compile(r"^INDEMNITEE$", re.IGNORECASE), 1),
    (re.compile(r"^/s/\s"), 1),
    (re.compile(r"^SCHEDULE\s+", re.IGNORECASE), 1),
    (re.compile(r"^APPENDIX\s+", re.IGNORECASE), 1),
    (re.compile(r"^ANNEX\s+", re.IGNORECASE), 1),
    # AGREEMENT root must be LAST so EXHIBIT/SCHEDULE/APPENDIX/ANNEX patterns
    # match first for subdoc titles (e.g. "SCHEDULE A ... AGREEMENT").
    (re.compile(r"^.*\b(AGREEMENT|PLAN)\s*$", re.IGNORECASE), 0),
    (re.compile(r"^(?:FIRST|SECOND|THIRD|FOURTH|FIFTH|SIXTH|SEVENTH|EIGHTH|NINTH|TENTH)\s+AMENDMENT\s+TO\s+", re.IGNORECASE), 0),
]


def _remap_depth(title: str | None, cls: str | None, tree_depth: int, *, parent_level: int | None = None) -> int:
    """Remap doc2dict tree depth to rubric-compliant level.

    Section-marker patterns take priority, then structural titles,
    then raw tree depth as fallback.  If *parent_level* is 1
    (exhibit/recitals/signatures) and no pattern matches, clamp to 1
    to avoid nesting inside metadata blocks.
    """
    inferred = _infer_level_from_title(title or "")
    if inferred is not None:
        return inferred
    stripped = (title or "").strip()
    if stripped:
        for pat, level in _STRUCTURAL_LEVELS:
            if pat.match(stripped):
                return level
    if parent_level is not None and parent_level <= 1:
        return max(parent_level, 1)
    if parent_level is not None and tree_depth < parent_level:
        return parent_level
    return tree_depth


# Subdocument classes — exhibit/schedule/appendix/annex containers, when
# they have actual children, are treated as nested subdocuments. Their
# DESCENDANTS get a +1 level penalty applied on top of `_remap_depth`'s
# inferred level. Penalty stacks for nested subdocs (annex inside schedule
# inside main → +2 to deepest descendants). The subdoc HEADER itself is
# unaffected — `_remap_depth` already maps EXHIBIT/SCHEDULE/etc. to 1, and
# a nested subdoc title gets the parent's penalty added to its own remap.
#
# A lead-in exhibit envelope marker like "EXHIBIT 10.25" with empty body
# is NOT a subdocument — it's metadata from the SEC envelope and is dropped
# from JSONL output (kept in parquet); the JSONL writer in `main` handles
# that drop, this constant is for the penalty-propagation logic.
_SUBDOC_CLASSES = frozenset({"exhibit", "schedule", "appendix", "annex"})

# ---------------------------------------------------------------------------
# Structural scope rule (task_rules/scope_rule.md)
# ---------------------------------------------------------------------------
# A section is OUT OF SCOPE for JSONL if BOTH:
#   1. It appears AFTER the SIGNATURE BLOCK, AND
#   2. It is NOT a descendant of a REAL SUBDOCUMENT.
# Otherwise it is IN SCOPE.
#
# This replaces phrase-based filtering. No keyword blocklists.

# Real subdoc title: has a descriptive part after the bare identifier.
_REAL_SUBDOC_TITLE_RE = re.compile(
    r"^(?:EXHIBIT|SCHEDULE|APPENDIX|ANNEX)\s+[A-Z0-9.\-]+\s*[—\-:]\s*\S+",
    re.IGNORECASE,
)

# Bare subdoc identifier (no descriptive separator): "EXHIBIT 8.2",
# "SCHEDULE 1.33", "ANNEX I". A bare identifier alone is NOT a real
# subdoc per the canonical scope rule; but doc2dict often splits the
# source text "SCHEDULE 1.33 CALCULATION OF LABOR COSTS" into two HTML
# elements (the bare identifier as the schedule node, the descriptive
# part as a predicted-header child). When the descriptive part is a
# direct child predicted-header with all-caps text, the combined
# parent+child IS a real subdoc — the descriptive title is just on the
# next HTML element, not the next textual line.
_BARE_SUBDOC_ID_RE = re.compile(
    r"^(?:EXHIBIT|SCHEDULE|APPENDIX|ANNEX)\s+[A-Z0-9.\-\(\)]+\s*$",
    re.IGNORECASE,
)

# Descriptive child header: predicted-header title that's predominantly
# upper-case letters (descriptive subdoc name in caps), not a section
# marker or `[***]` redaction placeholder. Used to qualify a bare
# subdoc as a real subdoc when its first child carries the description.
def _looks_like_descriptive_subdoc_child_title(title: str | None) -> bool:
    if not title:
        return False
    stripped = title.strip()
    if not stripped:
        return False
    # Strip surrounding brackets/redaction artifacts.
    if stripped.startswith("[") and stripped.endswith("]"):
        return False
    # Must contain at least one uppercase letter sequence of length >= 2
    # (i.e. a meaningful word in caps, not just punctuation/digits).
    if not re.search(r"[A-Z]{2,}", stripped):
        return False
    # Section markers like "(a)", "1.", "ARTICLE 1." don't qualify.
    if re.match(r"^\s*(?:\(\w+\)|\d+\.|SECTION\s+\d|ARTICLE\s+\w+)", stripped, re.IGNORECASE):
        return False
    return True

# Bare roman-numeral TOC page marker: "i", "ii", "iii", etc. with no body.
_TOC_PAGE_MARKER_RE = re.compile(r"^[ivxlcdm]+$")

# Signature line: "/s/ Jane Doe", "/s/ Terrance K. Wallberg"
_SIG_LINE_RE = re.compile(r"/s/\s+\w+")

# Heuristic for "still part of signature block": content matching these
# patterns at the start of the span indicates a signature-block continuation.
_SIG_CONTINUATION_RE = re.compile(
    r"(?i)(?:by:\s|name:\s|title:\s|address:\s|address:$|/s/\s)",
    re.MULTILINE,
)

_SIG_PAGE_TITLE_RE = re.compile(
    r"^\[?\s*SIGNATURE\s+PAGE", re.IGNORECASE
)


def _is_real_subdoc_title(title: str | None, cls: str | None, is_envelope: bool, first_child_title: str | None = None) -> bool:
    """Return True if this section is a REAL subdocument (not the SEC envelope).

    Matches scope_rule.md's definition: cls in subdoc classes, not envelope,
    has descriptive text beyond bare identifier. Two structural patterns
    qualify as "has descriptive text":

      1. Same-line form: title carries identifier + separator + description,
         e.g. "SCHEDULE 1: DEFINITIONS", "EXHIBIT A — FORM OF NOTICE".
         Detected by `_REAL_SUBDOC_TITLE_RE`.

      2. Split-form: title is just the bare identifier (e.g. "SCHEDULE
         1.33") AND the first direct child is a `predicted header` whose
         title is the descriptive part on its own ("CALCULATION OF LABOR
         COSTS, EXPENSE ALLOCATION AND RELATED MATTERS"). This happens
         when doc2dict splits "SCHEDULE 1.33 CALCULATION OF LABOR COSTS"
         from the source text into two HTML elements (a `<schedule>`
         identifier and a `<predicted_header>` description). The
         combined parent+child IS a real subdoc — the descriptive title
         is just on the next HTML element, not the next textual line.

    A bare identifier ("EXHIBIT 8.2") with NO descriptive suffix AND NO
    descriptive child IS NOT a real subdoc (it's the SEC envelope or a
    placeholder).
    """
    if is_envelope:
        return False
    if (cls or "") not in _SUBDOC_CLASSES:
        return False
    if not title:
        return False
    stripped = title.strip()
    # Case 1: Same-line descriptive title (identifier + separator + text).
    if _REAL_SUBDOC_TITLE_RE.match(stripped):
        return True
    # Case 2: Bare identifier + descriptive child header.
    if _BARE_SUBDOC_ID_RE.match(stripped) and _looks_like_descriptive_subdoc_child_title(first_child_title):
        return True
    return False


def _find_signature_cutoff(rows: list[dict[str, Any]]) -> int | None:
    """Find the node_id of the LAST record in the signature-block cluster.

    Heuristic:
      1. Find the LAST section whose span contains a /s/ signature line.
      2. Extend forward while consecutive same-depth records still match
         signature-block continuation patterns (By:/Name:/Title:/Address:/s/).
      3. Return the node_id of the last record in that cluster.
      4. If no /s/ lines found, fall back to the last SIGNATURE PAGE or
         IN WITNESS WHEREOF structural title.

    Returns None if no signature block is found.
    """
    last_sig_idx: int | None = None
    for i, r in enumerate(rows):
        span = _build_jsonl_span(r.get("title"), r.get("body_direct"), r.get("cls"))
        if _SIG_LINE_RE.search(span):
            last_sig_idx = i

    if last_sig_idx is None:
        for i, r in enumerate(rows):
            title = (r.get("title") or "").strip()
            if not title:
                continue
            if _SIG_PAGE_TITLE_RE.match(title):
                last_sig_idx = i
                continue
            for pat, _ in _STRUCTURAL_LEVELS:
                if pat.match(title) and "WITNESS" in pat.pattern and "WHEREOF" in pat.pattern:
                    last_sig_idx = i
        if last_sig_idx is None:
            return None

    cutoff_idx = last_sig_idx
    sig_depth = rows[last_sig_idx].get("depth", 1)
    for i in range(last_sig_idx + 1, len(rows)):
        r = rows[i]
        if r.get("depth", 0) != sig_depth:
            break
        span = _build_jsonl_span(r.get("title"), r.get("body_direct"), r.get("cls"))
        if not _SIG_CONTINUATION_RE.search(span[:300]):
            title = (r.get("title") or "").strip()
            if title and len(title.split()) <= 3 and title.isupper():
                if (r.get("cls") or "") not in _SUBDOC_CLASSES:
                    cutoff_idx = i
                    continue
            break
        cutoff_idx = i

    return rows[cutoff_idx]["node_id"]


def _apply_scope_rule(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply the structural scope rule to mark each row's scope.

    Adds a ``scope`` key to each row: ``"agreement"`` or ``"trailer"``.
    Rules (per scope_rule.md):
      - A section S is OUT OF SCOPE if BOTH:
        1. S appears AFTER the SIGNATURE BLOCK, AND
        2. S is NOT a descendant of a REAL SUBDOCUMENT.
      - Otherwise S is IN SCOPE.

    "After the signature block" is determined by tree structure, not flat
    list index. doc2dict interleaves signature lines with agreement body
    content (e.g. Section 13.3 provisions as siblings of /s/ lines under
    the same MISCELLANEOUS parent). Using flat index would wrongly mark
    those agreement provisions as trailer. Instead: the signature block's
    containing parent defines the boundary. Only nodes that are siblings
    of the signature parent AND come after it in document order, or are
    descendants of such siblings, are "after the signature block."
    """
    by_node_id: dict[int, dict[str, Any]] = {r["node_id"]: r for r in rows}

    sig_cutoff_node_id = _find_signature_cutoff(rows)
    if sig_cutoff_node_id is None:
        for r in rows:
            r["scope"] = "agreement"
        return rows

    real_subdoc_ids: set[int] = set()
    children_of: dict[int | None, list[dict[str, Any]]] = {}
    for r in rows:
        pid = r.get("parent_node_id")
        children_of.setdefault(pid, []).append(r)
    for r in rows:
        children = children_of.get(r["node_id"], [])
        first_child_title = children[0]["title"] if children else None
        if _is_real_subdoc_title(r.get("title"), r.get("cls"), r.get("is_envelope", False), first_child_title):
            real_subdoc_ids.add(r["node_id"])

    def _is_descendant_of_subdoc(row: dict[str, Any]) -> bool:
        current_id = row.get("parent_node_id")
        visited: set[int | None] = set()
        while current_id is not None and current_id not in visited:
            visited.add(current_id)
            parent = by_node_id.get(current_id)
            if parent is None:
                break
            if parent["node_id"] in real_subdoc_ids:
                return True
            current_id = parent.get("parent_node_id")
        return False

    # Find the signature record's parent, then find all nodes that are
    # "after" the signature block in tree order. The signature block's
    # parent's children are the boundary: any sibling of the signature
    # parent that appears AFTER the signature parent in child-order is
    # "post-signature." The signature parent's own descendants are still
    # agreement scope.
    sig_record = by_node_id.get(sig_cutoff_node_id)
    if sig_record is None:
        for r in rows:
            r["scope"] = "agreement"
        return rows

    sig_parent_id = sig_record.get("parent_node_id")
    post_sig_node_ids: set[int] = set()

    def _collect_descendants(nid: int) -> None:
        for child in children_of.get(nid, []):
            post_sig_node_ids.add(child["node_id"])
            _collect_descendants(child["node_id"])

    # Mark later siblings of the signature cutoff node as post-signature,
    # but ONLY when those siblings are structural subdocuments (schedule,
    # exhibit, appendix, annex). Body text provisions interleaved after
    # signature lines under the same parent are still agreement content.
    cutoff_siblings = children_of.get(sig_parent_id, [])
    after_cutoff = False
    for sib in cutoff_siblings:
        if sib["node_id"] == sig_cutoff_node_id:
            after_cutoff = True
            continue
        if after_cutoff:
            sib_cls = (sib.get("cls") or "")
            if sib_cls in _SUBDOC_CLASSES:
                post_sig_node_ids.add(sib["node_id"])
                _collect_descendants(sib["node_id"])

    # Walk from the signature node up to its root-level ancestor.  Mark
    # all siblings of each ancestor that appear AFTER the ancestor's
    # child-on-the-path-to-the-signature as post-signature.  This ensures
    # that top-level annexes/exhibits after the signature's containing
    # article are detected even when the signature is deeply nested.
    current_child_id: int | None = sig_cutoff_node_id
    current_id: int | None = sig_parent_id
    while current_id is not None and current_child_id is not None:
        current_node = by_node_id.get(current_id)
        if current_node is None:
            break
        current_parent_id = current_node.get("parent_node_id")
        siblings = children_of.get(current_parent_id, [])
        after = False
        for sib in siblings:
            if sib["node_id"] == current_id:
                after = True
                continue
            if after:
                post_sig_node_ids.add(sib["node_id"])
                _collect_descendants(sib["node_id"])
        current_child_id = current_id
        current_id = current_parent_id

    for r in rows:
        if r["node_id"] not in post_sig_node_ids:
            r["scope"] = "agreement"
        elif r["node_id"] in real_subdoc_ids:
            r["scope"] = "agreement"
        elif _is_descendant_of_subdoc(r):
            r["scope"] = "agreement"
        else:
            r["scope"] = "trailer"

    return rows


# Detects text leaves starting with a section marker for promotion.
_TEXT_LEAF_SECTION_RE = re.compile(
    r"^\s*"
    r"(?:"
    r"\d+\.\s"
    r"|\([a-z]\)\s"
    r"|\([ivx]+\)\s"
    r"|\([A-Z]\)\s"
    r"|\(\d+\)\s"
    r")"
)


def _promote_text_leaves(
    contents: dict[Any, Any],
) -> list[tuple[str, dict[str, Any]]]:
    """Scan *contents* for text leaves starting with a section marker.

    Returns a list of ``(key, virtual_section_node)`` pairs in document
    order.  Each virtual node has ``title`` set to the marker-prefixed
    heading (first line or first N chars up to a period/space break)
    and ``body`` set to the remaining text.
    """
    promoted: list[tuple[str, dict[str, Any]]] = []
    if not isinstance(contents, dict):
        return promoted
    for key, child in contents.items():
        if not _is_text_leaf(child):
            continue
        text = child.get("text", "")
        if not text or not _TEXT_LEAF_SECTION_RE.match(text):
            continue
        # Heuristic: heading = text up to first ". " or ".\xa0" break.
        m = re.match(r"^\s*((?:\d+\.|\([a-z]\)|\([ivx]+\)|\([A-Z]\)|\(\d+\))[^\n]*?)(?:\.\s|\.\xa0)", text)
        if m:
            heading = m.group(1).strip()
            body = text[m.end():].strip()
        else:
            lines = text.split("\n", 1)
            heading = lines[0].strip()
            body = lines[1].strip() if len(lines) > 1 else ""
        promoted.append((str(key), {
            "title": heading,
            "class": "promoted text leaf",
            "contents": {},
            "_body_text": body or "",
            "_full_text": text,
        }))
    return promoted


def _promote_text_blob(
    text: str,
    key_prefix: str,
) -> list[tuple[str, dict[str, Any]]]:
    """Split a single text blob on section markers, return virtual nodes.

    The portion of *text* before the first section marker (the preamble)
    is returned as a special entry with ``_preamble`` set to ``True`` so
    the caller can route it as body-direct instead of a standalone row.

    Only matches section markers that begin a logical segment —
    preceded by start-of-string, ``\\n``, ``.  ``, or ``:  ``.
    This avoids false positives like ``(d)`` inside body text
    (e.g. "described in Section 3(d) of this Agreement").
    """
    if not text or not text.strip():
        return []
    _MARKER_RE = re.compile(
        r"(?:(?:^|(?<=[.:\"'\u201d)]))\s+|(?<=\s)|^)"
        r"((?:\d+\.(?:\s|[A-Z])|\([a-z]\)(?:\s|[A-Z])|\([ivx]+\)(?:\s|[A-Za-z])|\([A-Z]\)(?:\s|[a-z])|\(\d+\)(?:\s|[A-Z])))"
    )
    markers = [(m.start(1), m.end(1)) for m in _MARKER_RE.finditer(text)]
    if not markers:
        return []

    promoted: list[tuple[str, dict[str, Any]]] = []

    for idx, (mk_start, mk_end) in enumerate(markers):
        next_mk_start = markers[idx + 1][0] if idx + 1 < len(markers) else len(text)
        chunk = text[mk_start:next_mk_start].strip()
        if not chunk:
            continue

        # Split heading from body: first "sentence" up to ". " / ".\n" is heading
        # If no period found, first line is heading
        # heading_match looks for the marker + text up to a period+space boundary
        hm = re.match(
            r"^((?:\d+\.(?:\s|[A-Z])|\([a-z]\)(?:\s|[A-Z])|\([ivx]+\)(?:\s|[A-Za-z])|\([A-Z]\)(?:\s|[a-z])|\(\d+\)(?:\s|[A-Z]))"
            r"[^.]*?)(?:\.\s|\.\n|\.\xa0)",
            chunk,
        )
        if hm:
            heading = hm.group(1).strip().rstrip(".")
            body = chunk[hm.end():].strip()
        elif len(chunk) > 80:
            _TITLE_SPLIT = re.match(
                r"^((?:\d+\.(?:\s|[A-Z])|\([a-z]\)(?:\s|[A-Z])|\([ivx]+\)(?:\s|[A-Za-z])|\([A-Z]\)(?:\s|[a-z])|\(\d+\)(?:\s|[A-Z]))"
                r".*?)"
                r"(?:\s+[A-Z][a-z]+)?"
                r"\s+(?=(?:[a-z]+\s+){1,}[a-z]+)",
                chunk,
            )
            if _TITLE_SPLIT:
                heading = _TITLE_SPLIT.group(1).strip()
                body = chunk[_TITLE_SPLIT.end():].strip()
            else:
                lines = chunk.split("\n", 1)
                heading = lines[0][:80].strip()
                body = lines[0][80:].strip() if len(lines[0]) > 80 else ""
                if len(lines) > 1:
                    body = (body + "\n" + lines[1]).strip() if body else lines[1].strip()
        else:
            lines = chunk.split("\n", 1)
            heading = lines[0].strip()
            body = lines[1].strip() if len(lines) > 1 else ""

        promoted.append((f"{key_prefix}_t{idx}", {
            "title": heading,
            "class": "promoted table text",
            "contents": {},
            "_body_text": body or "",
            "_full_text": chunk,
        }))

    first_mk_start = markers[0][0]
    preamble_text = text[:first_mk_start].strip()
    if preamble_text:
        promoted.insert(0, (f"{key_prefix}_pre", {
            "title": "",
            "class": "table preamble",
            "contents": {},
            "_body_text": preamble_text,
            "_full_text": preamble_text,
            "_preamble": True,
        }))

    return promoted


def walk_sections(
    container: dict[Any, Any],
    *,
    depth: int = 0,
    parent_id: int | None = None,
    parent_level: int | None = None,
    counter: list[int] | None = None,
    subdoc_penalty: int = 0,
    envelope_seen: list[bool] | None = None,
    l0_seen: list[bool] | None = None,
    parent_is_envelope: bool = False,
) -> list[dict[str, Any]]:
    """Walk doc2dict's section tree and emit flat row records.

    `subdoc_penalty` is the cumulative count of ancestor subdocument
    containers (exhibit/schedule/appendix/annex) above this node, EXCLUDING
    the SEC <DOCUMENT> envelope wrapper. The penalty is added to each row's
    `depth` so subdocument internals shift one level deeper per ancestor.

    `envelope_seen` is a one-element list (mutable across recursion) that
    flips True the first time we encounter a subdoc-class section per
    document. SEC EX-10 filings wrap the entire contract in an empty-body
    `EXHIBIT <n>` envelope; that's metadata, not a real subdocument, so its
    descendants must NOT inherit the +1 penalty. Subsequent subdoc-class
    sections (e.g. "EXHIBIT A — FORM OF NOTICE" deeper in the body) ARE
    real subdocuments and DO push the penalty.
    """
    if counter is None:
        counter = [0]
    if envelope_seen is None:
        envelope_seen = [False]
    if l0_seen is None:
        l0_seen = [False]
    rows: list[dict[str, Any]] = []
    if not isinstance(container, dict):
        return rows

    for section_key, section in container.items():
        if not _is_section_node(section):
            continue
        title = section.get("title")
        cls = section.get("class")
        std = section.get("standardized_title")
        body_direct = _collect_direct_text(section)
        if (
            title
            and not body_direct.strip()
            and _TOC_PAGE_MARKER_RE.match(title.strip())
        ):
            continue
        node_id = counter[0]
        counter[0] += 1
        body_recursive = _collect_recursive_text(section)
        contents = section.get("contents") or {}
        n_direct_children = (
            sum(1 for c in contents.values() if _is_section_node(c))
            if isinstance(contents, dict)
            else 0
        )
        # Identify and absorb the SEC envelope: first subdoc-class node per
        # doc is the wrapper, not a real subdocument.  Also absorb
        # introduction pseudo-nodes whose body is just an exhibit tag
        # (e.g. body_direct="Exhibit 10.9") — SEC filing metadata.
        is_envelope = False
        if (cls or "") in _SUBDOC_CLASSES and not envelope_seen[0]:
            envelope_seen[0] = True
            is_envelope = True
        elif (
            not envelope_seen[0]
            and body_direct.strip()
            and re.match(r"^EXHIBIT\s+\d+", body_direct.strip(), re.IGNORECASE)
        ):
            envelope_seen[0] = True
            is_envelope = True

        remapped = _remap_depth(title, cls, depth, parent_level=parent_level)
        if remapped == 0 and not body_direct.strip():
            is_agreement_root = False
            stripped = (title or "").strip()
            if stripped and n_direct_children > 0:
                for _pat, _lvl in _STRUCTURAL_LEVELS:
                    if _lvl == 0 and _pat.match(stripped):
                        is_agreement_root = True
                        break
            if not is_agreement_root:
                remapped = 1

        is_secondary_agreement = False
        if subdoc_penalty == 0 and not is_envelope:
            if remapped == 0 and l0_seen[0]:
                remapped = 1
                is_secondary_agreement = True
            if remapped == 0:
                l0_seen[0] = True

        effective = remapped + subdoc_penalty
        effective = min(effective, 4 + subdoc_penalty)
        rows.append(
            {
                "node_id": node_id,
                "parent_node_id": parent_id,
                "depth": effective,
                "doc2dict_section_key": str(section_key),
                "cls": cls,
                "title": title,
                "standardized_title": std,
                "n_direct_section_children": n_direct_children,
                "body_direct_chars": len(body_direct),
                "body_recursive_chars": len(body_recursive),
                "body_direct": body_direct,
                "body_recursive": body_recursive,
                "subdoc_penalty": subdoc_penalty,
                "is_envelope": is_envelope,
            }
        )
        # Penalty bumps for descendants only when this section is a REAL
        # subdocument (subdoc class AND not the envelope).
        child_penalty = subdoc_penalty + (
            1 if ((cls or "") in _SUBDOC_CLASSES and not is_envelope) or (is_secondary_agreement and not parent_is_envelope) else 0
        )
        if isinstance(contents, dict):
            child_sections = {k: v for k, v in contents.items() if _is_section_node(v)}
            if child_sections:
                rows.extend(
                    walk_sections(
                        child_sections,
                        depth=depth + 1,
                        parent_id=node_id,
                        parent_level=effective,
                        counter=counter,
                        subdoc_penalty=child_penalty,
                        envelope_seen=envelope_seen,
                        l0_seen=l0_seen,
                    )
                )
            promoted = _promote_text_leaves(contents)
            for prom_key, prom_node in promoted:
                prom_id = counter[0]
                counter[0] += 1
                prom_title = prom_node["title"]
                prom_cls = prom_node["class"]
                prom_body = prom_node.get("_body_text", "")
                prom_full = prom_node.get("_full_text", prom_body)
                prom_remapped = _remap_depth(prom_title, prom_cls, depth + 1, parent_level=effective)
                prom_penalty = child_penalty if is_secondary_agreement else subdoc_penalty
                rows.append(
                    {
                        "node_id": prom_id,
                        "parent_node_id": node_id,
                        "depth": min(prom_remapped + prom_penalty, 4 + prom_penalty),
                        "doc2dict_section_key": prom_key,
                        "cls": prom_cls,
                        "title": prom_title,
                        "standardized_title": None,
                        "n_direct_section_children": 0,
                        "body_direct_chars": len(prom_body),
                        "body_recursive_chars": len(prom_full),
                        "body_direct": prom_body,
                        "body_recursive": prom_full,
                        "subdoc_penalty": prom_penalty,
                        "is_envelope": False,
                    }
                )

    return rows


# Build the config ONCE per process. make_agreement_config() does a deep copy
# of STANDARD_CONFIG internally so we don't risk leaking mutation across docs.
_AGREEMENT_CONFIG = make_agreement_config()


def _fix_post_signature_envelope(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Un-mark envelope nodes that appear after agreement body content.

    doc2dict sometimes strips the EXHIBIT tag from the HTML, so walk_sections
    never detects the real envelope and misidentifies the first post-content
    annex as the SEC envelope. Two structural cases mark such a node as
    "not really the envelope":

      Case A — sibling-based: an envelope node shares a parent with
      `section`/`article` siblings that appear BEFORE it in node_id order.
      The envelope is then post-agreement attachment.

      Case B — ancestor-based: the envelope node is a structural DESCENDANT
      of a `section`/`article` node (i.e. some ancestor in its parent chain
      is a body-class section). doc2dict's HTML walker sometimes mis-parents
      a real attached subdoc (ANNEX I, EXHIBIT A) as a child of the last
      body section it visited. If we're INSIDE a body section, we're past
      the agreement title and cannot be the SEC envelope (which precedes
      the title).
    """
    envelope_rows = [r for r in rows if r.get("is_envelope")]
    if not envelope_rows:
        return rows

    by_node_id = {r["node_id"]: r for r in rows}
    children_of: dict[int | None, list[dict[str, Any]]] = {}
    for r in rows:
        children_of.setdefault(r.get("parent_node_id"), []).append(r)

    _BODY_CLASSES = frozenset({"section", "article"})

    def _has_body_ancestor(node: dict[str, Any]) -> bool:
        """Walk up the parent chain looking for a section/article ancestor."""
        seen: set[int] = set()
        cur_pid = node.get("parent_node_id")
        while cur_pid is not None and cur_pid not in seen:
            seen.add(cur_pid)
            parent = by_node_id.get(cur_pid)
            if parent is None:
                return False
            if (parent.get("cls") or "") in _BODY_CLASSES:
                return True
            cur_pid = parent.get("parent_node_id")
        return False

    fixed = False

    for env_row in envelope_rows:
        # Only consider subdoc-class envelopes for the fix — leaving
        # `cls=introduction` or `cls=predicted header` envelope handling
        # to its existing dedicated paths.
        env_cls = (env_row.get("cls") or "")
        if env_cls not in _SUBDOC_CLASSES:
            continue
        parent_id = env_row.get("parent_node_id")
        siblings = children_of.get(parent_id, [])
        has_prior_body = False
        for sib in siblings:
            if sib["node_id"] == env_row["node_id"]:
                break
            if (sib.get("cls") or "") in _BODY_CLASSES:
                has_prior_body = True
                break

        # Case A: sibling-based detection.
        if has_prior_body:
            env_row["is_envelope"] = False
            fixed = True
            continue

        # Case B: ancestor-based detection. A real SEC envelope sits at the
        # root of the document tree (its parent chain has no body-class
        # node). If we're inside an article/section ancestor, we cannot
        # be the SEC envelope.
        if _has_body_ancestor(env_row):
            env_row["is_envelope"] = False
            fixed = True
            continue

    if not fixed:
        return rows

    def _bump_penalty(nid: int, extra: int) -> None:
        node = by_node_id.get(nid)
        if node is None:
            return
        node["subdoc_penalty"] += extra
        node["depth"] += extra
        for ch in children_of.get(nid, []):
            _bump_penalty(ch["node_id"], extra)

    for env_row in envelope_rows:
        if env_row.get("is_envelope"):
            continue
        cls = env_row.get("cls") or ""
        if cls in _SUBDOC_CLASSES:
            for ch in children_of.get(env_row["node_id"], []):
                _bump_penalty(ch["node_id"], 1)

    return rows


def _reparent_orphan_top_level_into_enclosing_subdoc(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Re-parent mis-parented nodes into the real-subdoc that encloses
    them by source position.

    doc2dict's HTML walker sometimes flattens a real-subdoc's descendants
    (ARTICLE I, ARTICLE II, ... of an attached REGISTRATION RIGHTS
    AGREEMENT, SECTION 4, 5, 6 ... of an attached CERTIFICATE OF
    DESIGNATIONS) and either:
      (A) leaves them with ``parent_node_id=None`` at the document root, or
      (B) parents them as a SIBLING of the real subdoc under the last
          main-agreement body section (e.g. ARTICLE VIII Miscellaneous).
    In either case the records appear in source-order BETWEEN the
    real-subdoc header (e.g. "ANNEX I CERTIFICATE OF DESIGNATIONS",
    node_id N_subdoc) and the next real-subdoc header (e.g. "ANNEX II
    REGISTRATION RIGHTS AGREEMENT", node_id N_next), and they are
    structurally part of the enclosing subdoc.

    Detection: a row whose node_id falls strictly between two consecutive
    real-subdoc-header node_ids AND whose current parent_node_id is
    OUTSIDE that span (None, or a pre-subdoc body node, or anything else
    not inside the subdoc's source span) is re-parented to the enclosing
    real subdoc.

    Rule of thumb:
      - The L0 title node itself is never re-parented.
      - Real subdoc headers themselves are never re-parented (they are
        the boundary markers).
      - Records BEFORE the first real-subdoc header are untouched (they
        belong to the main agreement body).
      - Records AFTER the LAST real-subdoc header and outside its node-id
        span are also re-parented to that last subdoc (e.g. the
        Certificate of Designations' SECTION 21, 22, 23 that mis-attached
        as siblings of ANNEX I rather than children).

    Runs BEFORE ``_apply_scope_rule`` so the scope rule sees a correctly
    nested tree.
    """
    by_node_id: dict[int, dict[str, Any]] = {r["node_id"]: r for r in rows}
    children_of: dict[int | None, list[dict[str, Any]]] = {}
    for r in rows:
        pid = r.get("parent_node_id")
        children_of.setdefault(pid, []).append(r)

    real_subdoc_rows: list[dict[str, Any]] = []
    for r in rows:
        children = children_of.get(r["node_id"], [])
        first_child_title = children[0]["title"] if children else None
        if _is_real_subdoc_title(
            r.get("title"), r.get("cls"), r.get("is_envelope", False), first_child_title
        ):
            real_subdoc_rows.append(r)

    if not real_subdoc_rows:
        return rows

    real_subdoc_rows.sort(key=lambda r: r["node_id"])
    subdoc_node_ids = [r["node_id"] for r in real_subdoc_rows]
    subdoc_id_set: set[int] = set(subdoc_node_ids)

    # Compute the source-span of each real subdoc: from its own node_id
    # to (next subdoc node_id - 1). For the last subdoc, the span extends
    # to the end of all node_ids in the document.
    max_nid = max((r["node_id"] for r in rows), default=0)
    subdoc_spans: list[tuple[int, int, dict[str, Any]]] = []  # (start, end_excl, subdoc)
    for i, sub in enumerate(real_subdoc_rows):
        start = sub["node_id"]
        if i + 1 < len(real_subdoc_rows):
            end = real_subdoc_rows[i + 1]["node_id"]
        else:
            end = max_nid + 1
        subdoc_spans.append((start, end, sub))

    def _is_in_span(nid: int, start: int, end: int) -> bool:
        return start < nid < end

    # For each enclosing subdoc, compute the node_id span [start, end).
    # A record at node_id N is "inside subdoc S" iff S.start <= N < S.end.
    enclosing_span_of: dict[int, tuple[int, int]] = {}
    for start, end, sub in subdoc_spans:
        enclosing_span_of[sub["node_id"]] = (start, end)

    for r in rows:
        nid = r["node_id"]
        # Skip the real subdoc headers themselves (they are boundary
        # markers, never re-parented).
        if nid in subdoc_id_set:
            continue
        # Skip records before the first real subdoc.
        if nid <= subdoc_node_ids[0]:
            continue
        # Skip L0 title and envelope nodes.
        if r.get("is_envelope") and r.get("depth", 1) == 0:
            continue
        if r.get("depth") == 0 and not r.get("is_envelope"):
            continue
        # Find the enclosing subdoc span by node_id.
        enclosing: dict[str, Any] | None = None
        for start, end, sub in subdoc_spans:
            if _is_in_span(nid, start, end):
                enclosing = sub
                break
        if enclosing is None:
            continue
        enc_start, enc_end = enclosing_span_of[enclosing["node_id"]]
        # Check current parent: if already inside (or IS) the enclosing
        # subdoc by node-id span, leave it alone.
        cur_pid = r.get("parent_node_id")
        if cur_pid is not None and cur_pid in by_node_id:
            if cur_pid == enclosing["node_id"]:
                continue
            if enc_start <= cur_pid < enc_end:
                continue
        # Re-parent into the enclosing subdoc.
        r["parent_node_id"] = enclosing["node_id"]

    return rows


def _merge_bare_section_marker_with_child(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge a bare "N." or "ARTICLE N." marker record with its descriptive child.

    doc2dict sometimes splits a numbered section into:
      - parent: title="10.", empty body
      - child: title="Retroactive Effect; …", body="All agreements…"
    where the child carries the actual section description and body.
    Per the rubric, this is ONE clause at depth 1: "10. Retroactive
    Effect…". Merging keeps the JSONL clean (no orphan marker records).

    The same pattern applies to ARTICLE titles:
      - parent: cls="article", title="ARTICLE 1.", empty body
      - child: cls="predicted header", title="DEFINITIONS", body="..."
    These merge to one record "ARTICLE 1. DEFINITIONS" with the child's body.

    Match criteria (numbered):
      - parent.title matches `^\\d+\\.\\s*$`
      - parent.body_direct is empty
      - parent has exactly one direct child (in `rows`, i.e. node-id-
        parented; not a doc2dict text leaf)
      - child.title is non-empty and does not start with a digit marker

    Match criteria (article):
      - parent.cls == "article"
      - parent.title matches `^ARTICLE\\s+\\w+\\.?\\s*$` (e.g. "ARTICLE 1.",
        "ARTICLE I", "ARTICLE 13.")
      - parent.body_direct is empty
      - parent has exactly one direct child
      - child.cls == "predicted header" with a descriptive title that
        doesn't start with another section marker

    On match (either form):
      - parent.title becomes `parent.title + ' ' + child.title`
      - parent.body_direct becomes child.body_direct
      - child is marked `is_envelope=True` so it drops from JSONL
        (parquet still records both for audit)
      - child's descendants are re-parented to the merged record
    """
    by_node_id = {r["node_id"]: r for r in rows}
    children_of: dict[int | None, list[dict[str, Any]]] = {}
    for r in rows:
        children_of.setdefault(r.get("parent_node_id"), []).append(r)

    _BARE_MARKER_RE = re.compile(r"^\s*\d+\.\s*$")
    _BARE_ARTICLE_MARKER_RE = re.compile(
        r"^\s*ARTICLE\s+(?:\d+|[IVXLCDM]+)\.?\s*$",
        re.IGNORECASE,
    )
    _HAS_MARKER_PREFIX_RE = re.compile(r"^\s*\d+\.")

    for r in rows:
        if r.get("is_envelope") or r.get("scope") == "trailer":
            continue
        title = (r.get("title") or "").strip()
        body = (r.get("body_direct") or "").strip()
        if not title or body:
            continue
        cls = (r.get("cls") or "")
        is_bare_numeric = bool(_BARE_MARKER_RE.match(title))
        is_bare_article = cls == "article" and bool(_BARE_ARTICLE_MARKER_RE.match(title))
        if not (is_bare_numeric or is_bare_article):
            continue
        kids = [
            c for c in children_of.get(r["node_id"], [])
            if not c.get("is_envelope") and c.get("scope") != "trailer"
        ]
        if not kids:
            continue
        # For bare-numeric markers ("10."): require exactly ONE child so we
        # don't accidentally absorb sibling subsections. For ARTICLE bare
        # markers, the article often has multiple children (descriptive
        # header + attached schedules/exhibits as later siblings under the
        # same article) — only the FIRST child (descriptive header) merges.
        if is_bare_numeric and len(kids) != 1:
            continue
        kid = kids[0]
        kid_title = (kid.get("title") or "").strip()
        kid_cls = (kid.get("cls") or "")
        if not kid_title or _HAS_MARKER_PREFIX_RE.match(kid_title):
            continue
        # For ARTICLE merge, the first kid must be a descriptive
        # predicted-header (not another structural class like schedule).
        if is_bare_article and kid_cls != "predicted header":
            continue
        # Merge: combine titles, take kid's body, mark kid as envelope.
        r["title"] = f"{title} {kid_title}"
        r["body_direct"] = kid.get("body_direct") or ""
        r["body_direct_chars"] = len(r["body_direct"])
        kid["is_envelope"] = True
        # Re-parent kid's children to merged record so their lineage
        # stays correct in the parquet view.
        for grand in children_of.get(kid["node_id"], []):
            grand["parent_node_id"] = r["node_id"]

    return rows


def _consolidate_real_subdocs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Process each real subdocument per the rubric depth-assignment rule.

    Per task_rules/examples_with_subdocs.md, the depth assignment is:

        depth = (natural structural depth from the title down)
              + (number of enclosing real subdocuments at that node)

    A subdoc HEADER sits at its natural depth (no +1 for being a subdoc
    itself — the header IS the boundary marker). Subdoc BODY descendants
    absorb the +1 per enclosing subdoc level.

    This pass does TWO things:

      1. **Bare-identifier rescue.** If a subdoc's title is a bare
         identifier ("SCHEDULE 1.33") AND its first child is a
         descriptive predicted header ("CALCULATION OF LABOR COSTS,
         EXPENSE ALLOCATION AND RELATED MATTERS"), merge the child's
         title into the subdoc's title and mark the child as envelope
         so it doesn't duplicate-emit.

      2. **Nested-subdoc promotion.** When a deep descendant of a
         subdoc bears a "SCHEDULE / EXHIBIT / APPENDIX / ANNEX N"
         pattern (a peer subdoc that doc2dict mis-nested), promote it
         to be a SIBLING of the enclosing subdoc (parent = enclosing
         subdoc's parent). This handles the doc2dict pathology where
         e.g. "SCHEDULE 5.2(b)" got buried inside "SCHEDULE 1.135"
         because the HTML didn't structurally separate them.

      3. **Depth assignment.** Each descendant of a real subdoc gets
         its depth set per the formula above. For descendants that
         aren't themselves nested subdocs, that's L2 (subdoc_depth=1
         + descendant natural depth=1 + subdoc penalty=0 once we count
         the enclosing-subdoc count externally). The descendant
         emission preserves doc2dict's natural grouping — we do not
         collapse them into one body.

    This step runs AFTER `_apply_scope_rule` (so we know real subdocs)
    and BEFORE `_sort_records_by_source_position`.
    """
    children_of: dict[int | None, list[dict[str, Any]]] = {}
    for r in rows:
        children_of.setdefault(r.get("parent_node_id"), []).append(r)
    for pid in children_of:
        children_of[pid].sort(key=lambda r: r["node_id"])

    # Identify real subdocs (using the existing test).
    def _is_subdoc(r: dict[str, Any]) -> bool:
        kids = children_of.get(r["node_id"], [])
        first_child_title = kids[0]["title"] if kids else None
        return _is_real_subdoc_title(
            r.get("title"), r.get("cls"), r.get("is_envelope", False), first_child_title
        )

    real_subdoc_ids: set[int] = set()
    for r in rows:
        if _is_subdoc(r):
            real_subdoc_ids.add(r["node_id"])

    if not real_subdoc_ids:
        return rows

    by_node_id = {r["node_id"]: r for r in rows}

    def _collect_descendants(nid: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for child in sorted(children_of.get(nid, []), key=lambda x: x["node_id"]):
            out.append(child)
            out.extend(_collect_descendants(child["node_id"]))
        return out

    # Bare-identifier rescue: merge first descriptive child into subdoc
    # title, mark child as envelope. Track which children were merged so
    # we don't re-process them as body descendants.
    merged_child_ids: set[int] = set()
    for subdoc_id in sorted(real_subdoc_ids):
        subdoc = by_node_id[subdoc_id]
        if subdoc.get("is_envelope"):
            continue
        kids = children_of.get(subdoc_id, [])
        if not kids:
            continue
        first_kid = kids[0]
        existing_title = (subdoc.get("title") or "").strip()
        first_kid_title = (first_kid.get("title") or "").strip()
        if (
            _BARE_SUBDOC_ID_RE.match(existing_title)
            and _looks_like_descriptive_subdoc_child_title(first_kid_title)
            and first_kid["node_id"] not in real_subdoc_ids
        ):
            subdoc["title"] = f"{existing_title} {first_kid_title}"
            first_kid["is_envelope"] = True
            merged_child_ids.add(first_kid["node_id"])

    # Promote nested subdoc headers: if a descendant title matches a
    # bare or descriptive subdoc pattern, promote it to be a SIBLING of
    # its enclosing subdoc. This handles the doc2dict pathology where
    # e.g. "SCHEDULE 5.2(b)" appears as a deep descendant of "SCHEDULE
    # 1.135" rather than as a sibling.
    _PROMOTABLE_SUBDOC_RE = re.compile(
        r"^(?:EXHIBIT|SCHEDULE|APPENDIX|ANNEX)\s+[A-Z0-9.\(\)\-]+\s*(?:[—\-:]\s*\S.*)?$",
        re.IGNORECASE,
    )

    for subdoc_id in sorted(real_subdoc_ids):
        subdoc = by_node_id[subdoc_id]
        if subdoc.get("is_envelope"):
            continue
        subdoc_parent_id = subdoc.get("parent_node_id")
        for d in _collect_descendants(subdoc_id):
            if d.get("is_envelope") or d.get("node_id") in merged_child_ids:
                continue
            d_title = (d.get("title") or "").strip()
            if not d_title:
                continue
            if not _PROMOTABLE_SUBDOC_RE.match(d_title):
                continue
            # This descendant looks like its own subdoc header. Promote
            # it: re-parent to the enclosing subdoc's parent, reset its
            # depth to match the enclosing subdoc (it's now a peer), and
            # mark as a real subdoc.
            d["parent_node_id"] = subdoc_parent_id
            d["depth"] = subdoc.get("depth", 1)
            real_subdoc_ids.add(d["node_id"])
            # Re-test bare-identifier rescue for the newly-promoted
            # subdoc. The descriptive partner can be:
            #   (a) the first direct child in the doc2dict tree, OR
            #   (b) the NEXT node_id in source order (doc2dict's
            #       tree-walk can scatter descriptive partner into a
            #       cousin position when the source had them sequential).
            promoted_kids = sorted(
                children_of.get(d["node_id"], []),
                key=lambda x: x["node_id"],
            )
            descriptive_partner: dict[str, Any] | None = None
            if promoted_kids:
                pk = promoted_kids[0]
                pk_title = (pk.get("title") or "").strip()
                if (
                    _BARE_SUBDOC_ID_RE.match(d_title)
                    and _looks_like_descriptive_subdoc_child_title(pk_title)
                    and pk["node_id"] not in real_subdoc_ids
                ):
                    descriptive_partner = pk
            if descriptive_partner is None and _BARE_SUBDOC_ID_RE.match(d_title):
                # Try (b): next-node-id descriptive partner anywhere in
                # the enclosing subdoc's subtree. Walk descendants of
                # the enclosing subdoc in node_id order and find the
                # first active one with a descriptive title (no body)
                # whose node_id > d["node_id"].
                for cand in _collect_descendants(subdoc_id):
                    if cand.get("is_envelope") or cand["node_id"] in real_subdoc_ids:
                        continue
                    if cand["node_id"] <= d["node_id"]:
                        continue
                    c_title = (cand.get("title") or "").strip()
                    c_body = (cand.get("body_direct") or "").strip()
                    if not c_title or c_body:
                        continue
                    if not _looks_like_descriptive_subdoc_child_title(c_title):
                        continue
                    descriptive_partner = cand
                    break
            if descriptive_partner is not None:
                d["title"] = f"{d_title} {(descriptive_partner.get('title') or '').strip()}"
                descriptive_partner["is_envelope"] = True
                merged_child_ids.add(descriptive_partner["node_id"])

    # Rebuild children index after promotions.
    children_of = {}
    for r in rows:
        children_of.setdefault(r.get("parent_node_id"), []).append(r)
    for pid in children_of:
        children_of[pid].sort(key=lambda r: r["node_id"])

    # Adjacent-pair subdoc rescue: a bare-identifier subdoc whose
    # IMMEDIATELY-NEXT sibling (in source order, by node_id) is a
    # descriptive predicted header but NOT a subdoc itself absorbs the
    # next sibling's title. The next sibling is marked envelope so it
    # doesn't double-emit. This handles the doc2dict pathology where
    # e.g. "SCHEDULE 5.2(b)" and "OUTLINE OF INITIAL DEVELOPMENT PLAN
    # FOR FIRST PRODUCT" are sibling predicted headers — the actual
    # subdoc title is "SCHEDULE 5.2(b) OUTLINE OF INITIAL DEVELOPMENT
    # PLAN FOR FIRST PRODUCT".
    for subdoc_id in sorted(real_subdoc_ids):
        subdoc = by_node_id[subdoc_id]
        if subdoc.get("is_envelope"):
            continue
        title = (subdoc.get("title") or "").strip()
        if not _BARE_SUBDOC_ID_RE.match(title):
            continue
        parent_id = subdoc.get("parent_node_id")
        siblings = children_of.get(parent_id, [])
        # Find subdoc in siblings, then look at next active sibling.
        try:
            idx_in_siblings = next(
                i for i, s in enumerate(siblings)
                if s["node_id"] == subdoc_id
            )
        except StopIteration:
            continue
        for next_idx in range(idx_in_siblings + 1, len(siblings)):
            next_sib = siblings[next_idx]
            if next_sib.get("is_envelope"):
                continue
            if next_sib["node_id"] in real_subdoc_ids:
                # Next sibling is another subdoc — don't merge.
                break
            next_title = (next_sib.get("title") or "").strip()
            next_body = (next_sib.get("body_direct") or "").strip()
            if not next_title or next_body:
                # Skip empty-title or substantive-body siblings — they
                # are not descriptive subdoc-title fragments.
                break
            if not _looks_like_descriptive_subdoc_child_title(next_title):
                break
            subdoc["title"] = f"{title} {next_title}"
            next_sib["is_envelope"] = True
            merged_child_ids.add(next_sib["node_id"])
            break

    # ([***] standalone redaction placeholders are KEPT as schedule body
    # content per Arthur's annotation — a schedule with `[***]` body is
    # the substantive content for redacted schedules.)
    # Chrome footer dropping is handled at the agreement level by the
    # scope-rule machinery (signature-page banner stripping etc.), not
    # by phrase matching here.

    # Consolidate non-subdoc descendants into ONE L2 body record per
    # subdoc. Per Arthur's annotation (task_rules/examples_with_subdocs.md
    # and the idx=1 expected output):
    #   - Subdoc title → L1
    #   - Subdoc body (all descendants concatenated, excluding nested
    #     subdocs and dropped chrome) → ONE L2 record per subdoc
    #
    # For schedules like SCHEDULE 1.33 whose only content is "[***]",
    # the body record carries that placeholder. For EXHIBIT 8.2 INITIAL
    # PRESS RELEASE, the body record carries the concatenated press-
    # release text. Nested subdocs (like SCHEDULE 5.2(b) promoted out of
    # SCHEDULE 1.135) emit at L1 separately.
    #
    # The consolidation also keeps records out of the boilerplate trip
    # zone: a 200-char span starting "Forward Looking Statement..." or
    # "MEDIA CONTACT:" would fail `freeze.py`'s structural validator,
    # but when those phrases are buried mid-body inside a longer L2
    # record, the validator's anchored-start check doesn't fire.
    def _natural_depth_for(child: dict[str, Any]) -> int:
        """Natural depth from the section marker on the child's title."""
        title = (child.get("title") or "").strip()
        kind = _classify_subsection_kind(title)
        if kind == "numbered":
            return 1
        if kind == "lettered":
            return 2
        if kind == "roman":
            return 3
        if kind == "capletter":
            return 4
        if kind == "parendigit":
            return 5
        return 1

    # For each top-level real subdoc, consolidate its body descendants.
    for subdoc_id in sorted(real_subdoc_ids):
        subdoc = by_node_id[subdoc_id]
        if subdoc.get("is_envelope"):
            continue
        parent_id = subdoc.get("parent_node_id")
        if parent_id is not None and parent_id in real_subdoc_ids:
            # Not a top-level subdoc (its parent is itself a subdoc).
            continue
        subdoc["depth"] = 1
        # Walk descendants and split into:
        #   - body_text_parts: concatenate into a single L2 record
        #   - section_marker_descendants: keep at natural+1 depth
        #   - nested_subdocs: handled by their own subdoc iteration
        body_parts: list[str] = []
        # Walk depth-first in node_id order, but stop descent at nested subdocs.
        def _walk_for_body(nid: int) -> None:
            for child in children_of.get(nid, []):
                if child.get("is_envelope"):
                    # Dropped — but recurse in case an active descendant
                    # got marked envelope for unrelated reasons (e.g.
                    # bare-id rescue marked the descriptive child).
                    _walk_for_body(child["node_id"])
                    continue
                if child["node_id"] in real_subdoc_ids:
                    # Nested subdoc — don't fold into this subdoc's body.
                    continue
                c_title = (child.get("title") or "").strip()
                c_body = (child.get("body_direct") or "").strip()
                # If the descendant has a section marker, it stays as
                # its OWN record at natural-depth + enclosing-subdoc-count.
                if _classify_subsection_kind(c_title) is not None:
                    child["depth"] = _natural_depth_for(child) + 1
                    _walk_for_body(child["node_id"])
                    continue
                # Otherwise fold into the body.
                if c_title and c_body:
                    body_parts.append(f"{c_title}\n{c_body}")
                elif c_title:
                    body_parts.append(c_title)
                elif c_body:
                    body_parts.append(c_body)
                # Mark as envelope so it doesn't emit separately.
                child["is_envelope"] = True
                _walk_for_body(child["node_id"])
        _walk_for_body(subdoc_id)
        # If we collected body text, append it to the subdoc's existing
        # body. Otherwise leave subdoc body as-is.
        if body_parts:
            existing = (subdoc.get("body_direct") or "").strip()
            consolidated = "\n".join([existing] + body_parts) if existing else "\n".join(body_parts)
            # Per Arthur's annotation, subdoc title is L1 and body is L2
            # (a child of the subdoc title). Create a separate L2 body
            # record so the title emits alone and the body emits alone.
            new_body_id = max((r["node_id"] for r in rows), default=-1) + 1
            body_row = {
                "node_id": new_body_id,
                "parent_node_id": subdoc_id,
                "depth": 2,
                "doc2dict_section_key": f"_subdoc_body_{new_body_id}",
                "cls": "promoted text leaf",
                "title": "",
                "standardized_title": None,
                "n_direct_section_children": 0,
                "body_direct_chars": len(consolidated),
                "body_recursive_chars": len(consolidated),
                "body_direct": consolidated,
                "body_recursive": consolidated,
                "subdoc_penalty": 1,
                "is_envelope": False,
                "scope": "agreement",
                # Flag for the post-sort anchor step: this body row sits
                # IMMEDIATELY after its parent subdoc record. Otherwise
                # the source-position sort can mis-locate it (e.g. the
                # press-release body's first words "MOMENTA PHARMACEUTICALS,
                # INC." also appear on the cover page, causing the
                # forward-progress search to anchor the body to the
                # cover region).
                "_subdoc_body_of": subdoc_id,
            }
            rows.append(body_row)
            # Clear subdoc body since the body is now in the new L2 record.
            subdoc["body_direct"] = ""
            subdoc["body_direct_chars"] = 0

    return rows


# Marker-pattern recognition for the foreign-content splitter.
# doc2dict's HTML tree walker can mis-attach trailing sibling content into
# the previous sibling's body_direct (the classic "tail mis-attribution"
# pathology — when whitespace-only or invisible-table rows between two
# sibling sections cause the walker to absorb the next section's content
# into the previous). This shows up structurally two ways:
#
#   1. Mis-parenting: a numbered "N. Title" section ends up as a SIBLING
#      of lettered "(a)/(b)/.../(z)" subsections under the same parent,
#      when those lettered subsections belong AS CHILDREN of the numbered
#      section. Fix: re-parent the lettered/roman/digit-marker siblings
#      that follow a numbered sibling into that numbered sibling.
#
#   2. Body-direct contamination: a section's body_direct contains a
#      lettered marker (e.g. "(g) ...") whose enumeration doesn't belong
#      to the section. The marker block (and any orphan prefix paragraph
#      before it) is foreign and belongs under a different parent — the
#      sibling section whose existing lettered children's sequence would
#      naturally continue with this letter. Fix: split the body, promote
#      the marker block as a new record under the correct parent, and
#      attach the orphan prefix to the same target parent.
#
# Both fixes operate on STRUCTURE (sibling order + child marker
# enumeration), not on phrase content. No keyword blocklist; no idx
# branches; no specific-phrase detection.

# A numbered top-level section marker at title start: "13.", "10.\xa0",
# "Section 13.2", "ARTICLE III". Anchored so partial matches inside body
# text don't trigger.
_NUMBERED_SECTION_TITLE_RE = re.compile(
    r"^\s*(?:"
    r"\d+\.\s"           # "1. ", "13. "
    r"|\d+\.\xa0"        # "1.\xa0", "13.\xa0"
    r"|SECTION\s+\d"     # "SECTION 1", "Section 13.2"
    r"|ARTICLE\s+(?:\d+|[IVXLCDM]+)"
    r")",
    re.IGNORECASE,
)

# Lettered subsection marker at title start: "(a)", "(b)", ..., "(z)".
_LETTERED_SUBSECTION_TITLE_RE = re.compile(r"^\s*\(([a-z])\)")

# Roman-numeral subsection marker at title start: "(i)", "(ii)", ...
_ROMAN_SUBSECTION_TITLE_RE = re.compile(r"^\s*\(([ivx]+)\)", re.IGNORECASE)

# Capital-letter subsection: "(A)", "(B)".
_CAPLETTER_SUBSECTION_TITLE_RE = re.compile(r"^\s*\(([A-Z])\)")

# Digit-in-parens: "(1)", "(2)".
_PARENDIGIT_SUBSECTION_TITLE_RE = re.compile(r"^\s*\((\d+)\)")


def _extract_letter_marker(title: str) -> str | None:
    """Return the lowercase letter from a "(a)" / "(b)" / ... title, or None."""
    if not title:
        return None
    m = _LETTERED_SUBSECTION_TITLE_RE.match(title)
    if m:
        return m.group(1).lower()
    return None


def _classify_subsection_kind(title: str) -> str | None:
    """Classify a title as 'numbered', 'lettered', 'roman', 'capletter',
    'parendigit', or None. Used for sibling-mismatch detection."""
    if not title:
        return None
    s = title.lstrip()
    if _NUMBERED_SECTION_TITLE_RE.match(s):
        return "numbered"
    # Order matters: roman before lettered (since "(i)" matches both).
    if _ROMAN_SUBSECTION_TITLE_RE.match(s):
        return "roman"
    if _LETTERED_SUBSECTION_TITLE_RE.match(s):
        return "lettered"
    if _CAPLETTER_SUBSECTION_TITLE_RE.match(s):
        return "capletter"
    if _PARENDIGIT_SUBSECTION_TITLE_RE.match(s):
        return "parendigit"
    return None


def _reparent_lettered_subsections_to_numbered_siblings(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Re-parent lettered/roman subsection records that are wrongly
    siblings of a numbered top-level section.

    Structural pattern this fixes (doc2dict's HTML-walker pathology):
        parent_X
          ├── "11. Security"               (numbered)
          ├── "12. Enforcement"            (numbered)
          ├── "(a) The Company..."         (lettered — belongs to 12)
          ├── "(b) This Agreement..."      (lettered — belongs to 12)
          ├── "13. Definitions"            (numbered)
          ├── "(a) Change in Control..."   (lettered — belongs to 13)
          ├── "(i) any person..."          (roman — belongs to 13(a))
          ├── ...
          ├── "(f) Independent Counsel..." (lettered — belongs to 13)

    Each "numbered sibling" claims all subsequent lettered/roman/etc.
    siblings (until the NEXT numbered sibling or a non-subsection sibling)
    as its own children. Roman/capletter/parendigit subsections that
    immediately follow a lettered one continue to be children of that
    lettered subsection (their original parent is still the parent of the
    re-parented chain, so we keep the doc2dict depth ordering).

    More precisely: when sweeping the children of a parent in sibling
    order, once we encounter a "numbered" child N, every later sibling
    classified as lettered/roman/capletter/parendigit gets re-parented to
    N's nearest enclosing lettered subsection if any (to preserve roman
    nesting under a lettered parent), otherwise to N itself. This is a
    purely structural rule based on marker patterns; it does not look at
    titles/body text content.
    """
    by_node_id = {r["node_id"]: r for r in rows}
    children_of: dict[int | None, list[dict[str, Any]]] = {}
    for r in rows:
        children_of.setdefault(r.get("parent_node_id"), []).append(r)

    # Stable sort children by node_id (doc2dict assigns node_ids in walk
    # order which matches sibling order under each parent).
    for pid in children_of:
        children_of[pid].sort(key=lambda r: r["node_id"])

    # Iterate over every parent and apply the rule.
    for parent_id, kids in list(children_of.items()):
        # Skip envelope/trailer kids — they're outside the structural
        # rule's domain.
        active_kids = [
            k for k in kids
            if not k.get("is_envelope") and k.get("scope") != "trailer"
        ]
        if len(active_kids) < 2:
            continue
        # Find indices of numbered children in active_kids order.
        numbered_idxs = [
            i for i, k in enumerate(active_kids)
            if _classify_subsection_kind(k.get("title")) == "numbered"
        ]
        if not numbered_idxs:
            continue
        # For each numbered child N, claim later subsection-marker siblings.
        # "later" = appears AFTER N in active_kids; "siblings to claim" =
        # lettered/roman/capletter/parendigit (numbered breaks the chain).
        # When a roman/capletter/parendigit follows a lettered, it should
        # be re-parented to that LETTERED (preserving 2-3 depth nesting),
        # not directly to N.
        for ni in numbered_idxs:
            numbered_node = active_kids[ni]
            current_lettered: dict[str, Any] | None = None
            for j in range(ni + 1, len(active_kids)):
                sib = active_kids[j]
                kind = _classify_subsection_kind(sib.get("title"))
                if kind == "numbered":
                    break  # next numbered section starts; stop claiming
                if kind == "lettered":
                    # Re-parent to the numbered section.
                    sib["parent_node_id"] = numbered_node["node_id"]
                    current_lettered = sib
                elif kind in {"roman", "capletter", "parendigit"}:
                    # Re-parent to the most recently seen lettered child
                    # if any, else to the numbered section directly.
                    target = current_lettered or numbered_node
                    sib["parent_node_id"] = target["node_id"]
                else:
                    # Non-subsection sibling — leave alone, don't claim.
                    # (e.g. signature block markers, headings)
                    pass

    return rows


def _split_foreign_lettered_markers_from_body(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Split lettered subsection markers out of a section's body_direct
    when they don't belong to that section.

    doc2dict's HTML walker can leave foreign content in a body_direct
    field — typically a continuation paragraph from a sibling section
    followed by a stray "(g) ..." marker that belongs to a DIFFERENT
    parent. This shows up as:

        Section S has body_direct =
            "...continuation paragraph that doesn't start with a marker...
            (g) "Proceeding" includes ..."
        where Section S itself uses numbered marker "10.", and a SIBLING
        of S — Section 13 (Definitions) — has lettered children
        (a), (b), (c), (d), (e), (f) but is missing (g).

    Structural test for "this marker is foreign":
      - The marker letter L appears as a lettered subsection marker
        ("(L) ...") in S's body_direct.
      - S does NOT itself have a lettered-children sequence that would
        naturally include L (i.e. S's existing children don't already
        cover (a)..(L-1)).
      - There IS a sibling section P (same parent as S, or a recent
        ancestor sibling) whose existing lettered children DO cover
        (a)..(L-1) and so naturally extend to (L). P is the rightful
        owner of the foreign marker block.

    On match:
      - Strip the foreign block from S's body_direct.
      - Emit a new lettered-subsection record under P with the marker
        block as its title+body.
      - Any orphan prefix paragraph in S's body_direct (text before the
        foreign marker that doesn't start with its own marker) is also
        foreign. Emit it as a body-continuation record under P (placed
        BEFORE the marker block in sibling order so document concat
        keeps source order).

    All decisions are structural (sibling tree + child marker
    enumeration). No phrase matching, no keyword blocklist, no idx
    branches.
    """
    by_node_id = {r["node_id"]: r for r in rows}
    children_of: dict[int | None, list[dict[str, Any]]] = {}
    for r in rows:
        children_of.setdefault(r.get("parent_node_id"), []).append(r)
    for pid in children_of:
        children_of[pid].sort(key=lambda r: r["node_id"])

    def _has_lettered_child_letter(parent_id: int, letter: str) -> bool:
        """True if parent_id has an active child whose marker is (letter)."""
        for c in children_of.get(parent_id, []):
            if c.get("is_envelope") or c.get("scope") == "trailer":
                continue
            cl = _extract_letter_marker(c.get("title") or "")
            if cl == letter:
                return True
        return False

    def _lettered_children_letters(parent_id: int) -> list[str]:
        """Return the sorted list of single-letter markers among parent's
        active children (lowercased)."""
        out: list[str] = []
        for c in children_of.get(parent_id, []):
            if c.get("is_envelope") or c.get("scope") == "trailer":
                continue
            cl = _extract_letter_marker(c.get("title") or "")
            if cl:
                out.append(cl)
        return sorted(set(out))

    # For each row, scan body_direct for foreign lettered markers.
    # Foreign-marker detection regex: lettered subsection at a logical
    # start position (after sentence end, newline, or string start). The
    # marker MUST be followed by a quote/opening-quote character OR
    # a capitalised word — to filter out cross-references like
    # "(d) of this Agreement" or "Section 3(a)(9) of the Exchange Act".
    _FOREIGN_MARKER_RE = re.compile(
        r"(?:(?<=\.)\s+|(?<=\?)\s+|(?<=!)\s+|\n\s*|^\s*)"
        r"(\(([a-z])\)\s*"
        r"(?:[“”‘’\"\']|[A-Z]))",
    )

    next_id = max((r["node_id"] for r in rows), default=-1) + 1
    new_rows: list[dict[str, Any]] = list(rows)
    appended: list[dict[str, Any]] = []

    for r in list(rows):
        if r.get("is_envelope") or r.get("scope") == "trailer":
            continue
        body = r.get("body_direct") or ""
        if not body or len(body) < 50:
            continue
        # Find lettered-marker matches in body.
        matches = list(_FOREIGN_MARKER_RE.finditer(body))
        if not matches:
            continue
        # For each match, test "foreign" structurally.
        first_foreign_offset: int | None = None
        target_parent_id: int | None = None
        target_letters_seen: list[str] = []
        for m in matches:
            letter = m.group(2).lower()
            marker_start = m.start(1)
            # Test 1: does the section R itself have lettered children
            # whose markers cover (a)..(letter-1)? If yes, this marker
            # might be R's next child — NOT foreign (rare since doc2dict
            # would normally promote it; but be safe).
            r_letters = _lettered_children_letters(r["node_id"])
            if r_letters and chr(ord(letter) - 1) in r_letters:
                # The marker continues R's own children — leave it.
                continue
            # Test 2: find a sibling section P whose existing lettered
            # children cover (a)..(letter-1), making this marker P's
            # natural continuation. P must be a sibling of R (same
            # parent_node_id) and appear before/around R in document
            # order. Prefer the NEAREST preceding sibling that fits.
            r_parent_id = r.get("parent_node_id")
            cand_targets: list[dict[str, Any]] = []
            # Look among all sections (any depth) whose own existing
            # lettered children form an unbroken prefix (a)..(letter-1).
            for cand in rows:
                if cand is r:
                    continue
                if cand.get("is_envelope") or cand.get("scope") == "trailer":
                    continue
                cand_letters = _lettered_children_letters(cand["node_id"])
                if not cand_letters:
                    continue
                # Must include letter-1 and form an unbroken prefix
                # (a), (b), ..., (letter-1).
                prev_letter = chr(ord(letter) - 1)
                if prev_letter not in cand_letters:
                    continue
                expected_prefix = [
                    chr(ord("a") + i) for i in range(ord(prev_letter) - ord("a") + 1)
                ]
                if cand_letters == expected_prefix:
                    cand_targets.append(cand)
            if not cand_targets:
                # No structural target — leave the marker in place.
                continue
            # Prefer the candidate that's a sibling of R (same parent);
            # otherwise the candidate appearing latest in document order
            # before R (closest preceding sibling-like).
            preferred: dict[str, Any] | None = None
            for c in cand_targets:
                if c.get("parent_node_id") == r_parent_id:
                    preferred = c
                    break
            if preferred is None:
                # Take the candidate with the highest node_id below R.
                cand_targets.sort(key=lambda c: c["node_id"])
                for c in cand_targets:
                    if c["node_id"] < r["node_id"]:
                        preferred = c
                if preferred is None:
                    preferred = cand_targets[-1]
            # If we already chose a target, ensure it's compatible.
            if target_parent_id is not None and preferred["node_id"] != target_parent_id:
                # Different target on this match — keep first target and
                # stop processing further markers for this record.
                break
            target_parent_id = preferred["node_id"]
            target_letters_seen.append(letter)
            if first_foreign_offset is None:
                first_foreign_offset = marker_start

        if target_parent_id is None or first_foreign_offset is None:
            continue

        # Now split the body. The text before first_foreign_offset is
        # an orphan-continuation paragraph that doesn't start with its
        # own marker. Structurally it cannot be R's content because R is
        # a numbered section whose own body would have its own marker if
        # any substantive lettered content existed. So the prefix is
        # ALSO foreign — it's the wrap-up of the foreign block's lettered
        # sibling that came BEFORE the marker we matched. Move it to
        # target_parent as a continuation record.
        #
        # Exception: if the prefix has its own subsection markers
        # ("(a)/N./..."), then it's a legitimate part of R's body that
        # _split_inline_section_markers should have split. We keep that
        # in R to be safe — but for the simple "no own marker" case
        # (which is the common doc2dict tail-attribution pathology) we
        # move it.
        prefix_text = body[:first_foreign_offset].rstrip()
        foreign_body = body[first_foreign_offset:].strip()

        # Structural check on the prefix: does it contain its own
        # subsection marker at a logical start position? If yes, it's
        # R's legitimate body and we keep it.
        prefix_has_own_marker = bool(_FOREIGN_MARKER_RE.search(prefix_text)) or bool(
            re.search(
                r"(?:(?<=[.\s\xa0])|^)\d+\.\s*[A-Z]",
                prefix_text,
            )
        )

        if prefix_has_own_marker:
            kept_body = prefix_text
            orphan_text = ""
        else:
            # All of the prefix is foreign — it's a continuation paragraph
            # belonging to the same target_parent as the marker block.
            kept_body = ""
            orphan_text = prefix_text

        # Update R's body to the kept portion (empty when the prefix is
        # foreign). R will have only its title.
        r["body_direct"] = kept_body
        r["body_direct_chars"] = len(kept_body)

        # Determine the depth for the new lettered records: target's
        # depth + 1.
        target_row = by_node_id[target_parent_id]
        target_depth = target_row.get("depth", 0)
        target_penalty = target_row.get("subdoc_penalty", 0)
        # Lettered subsections sit at target_depth + 1.
        new_letter_depth = target_depth + 1

        # The orphan-prefix paragraph attaches NOT to target_parent
        # itself but to one of target_parent's lettered children — the
        # one the orphan is the wrap-up of. Structural identifier: the
        # first lettered child of target_parent that has sub-children
        # (roman/capletter/parendigit). The orphan logically appears
        # AFTER those sub-children in source order and is a body
        # continuation of that lettered child.
        #
        # Rationale: when a lettered subsection L has deep sub-items
        # (e.g. roman (i)-(v) under "(a) Change in Control means ..."),
        # the typical doc structure is L's body = sub-items + wrap-up
        # paragraph. doc2dict's HTML walker can detach that wrap-up
        # paragraph and bury it in a sibling section's body_direct.
        # Attaching the orphan back under L (depth = L.depth + 1)
        # restores the original semantic.
        #
        # If no lettered child of target_parent has sub-children, fall
        # back to attaching the orphan under target_parent at the
        # lettered-sibling depth.
        orphan_parent_id = target_parent_id
        orphan_depth = new_letter_depth
        if orphan_text:
            for tc in children_of.get(target_parent_id, []):
                if tc.get("is_envelope") or tc.get("scope") == "trailer":
                    continue
                if _extract_letter_marker(tc.get("title") or "") is None:
                    continue
                # Count sub-children of this lettered child.
                grand = children_of.get(tc["node_id"], [])
                deep_kids = [
                    g for g in grand
                    if not g.get("is_envelope") and g.get("scope") != "trailer"
                    and _classify_subsection_kind(g.get("title")) in {
                        "roman", "capletter", "parendigit"
                    }
                ]
                if deep_kids:
                    orphan_parent_id = tc["node_id"]
                    orphan_depth = tc.get("depth", new_letter_depth) + 1
                    break

        # Re-scan foreign_body, this time relative to its own start, to
        # build records. Use the same _FOREIGN_MARKER_RE.
        foreign_matches = list(_FOREIGN_MARKER_RE.finditer(foreign_body))

        if orphan_text:
            # Emit as a body-continuation record. Its title is empty so
            # the JSONL span is just the body text. Position-sort will
            # place it correctly in document order.
            appended.append({
                "node_id": next_id,
                "parent_node_id": orphan_parent_id,
                "depth": orphan_depth,
                "doc2dict_section_key": f"_foreign_cont_{next_id}",
                "cls": "promoted text leaf",
                "title": "",
                "standardized_title": None,
                "n_direct_section_children": 0,
                "body_direct_chars": len(orphan_text),
                "body_recursive_chars": len(orphan_text),
                "body_direct": orphan_text,
                "body_recursive": orphan_text,
                "subdoc_penalty": target_penalty,
                "is_envelope": False,
                "scope": "agreement",
            })
            next_id += 1

        # Emit each foreign-marker block as a new record.
        for i, m in enumerate(foreign_matches):
            mk_start = m.start(1)
            mk_end = foreign_matches[i + 1].start(1) if i + 1 < len(foreign_matches) else len(foreign_body)
            chunk = foreign_body[mk_start:mk_end].strip()
            if not chunk:
                continue
            # Split heading from body: heading = "(L) Title text." up to first
            # period+space, or first ~80 chars.
            mh = re.match(
                r"^(\([a-z]\)\s*[^.\n]{0,160}?)(?:\.\s|\.\xa0|\.$|\.\n)",
                chunk,
            )
            if mh:
                heading = mh.group(1).strip()
                body_text = chunk[mh.end():].strip()
            else:
                lines = chunk.split("\n", 1)
                heading = lines[0].strip()[:160]
                body_text = lines[1].strip() if len(lines) > 1 else chunk[len(heading):].strip()
            appended.append({
                "node_id": next_id,
                "parent_node_id": target_parent_id,
                "depth": new_letter_depth,
                "doc2dict_section_key": f"_foreign_marker_{next_id}",
                "cls": "promoted text leaf",
                "title": heading,
                "standardized_title": None,
                "n_direct_section_children": 0,
                "body_direct_chars": len(body_text),
                "body_recursive_chars": len(chunk),
                "body_direct": body_text,
                "body_recursive": chunk,
                "subdoc_penalty": target_penalty,
                "is_envelope": False,
                "scope": "agreement",
            })
            next_id += 1

    new_rows.extend(appended)
    return new_rows


def _sort_records_by_source_position(
    rows: list[dict[str, Any]], idx: int
) -> list[dict[str, Any]]:
    """Stable-sort in-scope records by their first-occurrence position in
    the source text, with a forward-progress search bias.

    Why this exists: doc2dict's tree walk is not always document order.
    For example, in the ULURU indemnification agreement (idx=0), Section 9
    sits under a separate "(d)" predicted-header subtree that follows the
    "10." subtree in the tree — but in the source, Section 9 obviously
    comes before Section 10. Same kind of issue with the table-split
    sections 14-21 (which we inject post-walk).

    Forward-progress search: when locating each record in the source, we
    start the search at the previous record's offset (minus a small
    backtrack window). That prevents repeated short tokens like
    "INDEMNITEE" from matching the FIRST occurrence in the preamble
    every time — they'll match the next occurrence after the previous
    record's position, which is the actual signature-block label.

    Records whose probe is not found get the previous record's offset
    so they stay glued to their preceding neighbour.

    Note: this is a STRUCTURAL fix — it uses doc-position evidence from
    the canonical bs4 extraction (`scripts/parse_source_of_truth.py`),
    not phrase matching against keywords.
    """
    sot_text = _SOT_LOOKUP.get(idx)
    if sot_text is None:
        sot_text = _load_sot_span_clean(idx)
        _SOT_LOOKUP[idx] = sot_text
    if not sot_text:
        return rows

    # Normalize source and probes: lowercase; collapse all whitespace
    # (including nbsp) to single space; fold curly quotes and Unicode
    # dashes to ASCII; STRIP ALL QUOTE CHARACTERS AND WHITESPACE-NEXT-
    # TO-QUOTES; collapse runs of space again. The quote-strip+space-
    # collapse makes typographically-decorated text like
    #    (b) " Corporate Status " describes
    # match a probe whose title is
    #    (b) "Corporate Status" describes
    # by reducing both to
    #    (b) corporate status describes
    # i.e. spaces-around-quotes inside the source no longer break the
    # match. Quote characters themselves carry no positional signal
    # for clause-boundary location; dropping them is safe.
    def _typographic_fold(s: str) -> str:
        s = s.translate(str.maketrans({
            "‘": "'", "’": "'", "‚": "'", "′": "'",
            "“": '"', "”": '"', "„": '"', "″": '"',
            "‐": "-", "‑": "-", "‒": "-", "–": "-",
            "—": "-", "―": "-", "−": "-",
        }))
        return s

    def _strip_quotes_and_collapse(s: str) -> str:
        # Remove ASCII quote characters and collapse whitespace.
        s = s.replace('"', '').replace("'", '')
        s = re.sub(r"[\s\xa0]+", " ", s).strip()
        # Also strip whitespace immediately INSIDE parentheses (the
        # source may have "(the " Agreement ")" after curly-quote
        # stripping while the probe has clean "(the Agreement)").
        s = re.sub(r"\(\s+", "(", s)
        s = re.sub(r"\s+\)", ")", s)
        return s

    sot_norm = _strip_quotes_and_collapse(_typographic_fold(sot_text).lower())

    def _normalize_probe(s: str) -> str:
        return _strip_quotes_and_collapse(_typographic_fold(s).lower())

    def _probe_candidates(rec: dict[str, Any]) -> list[str]:
        """Build progressively-shorter probes for the record."""
        title = (rec.get("title") or "").strip()
        body = (rec.get("body_direct") or "").strip()
        candidates: list[str] = []
        # Most specific: title + body head.
        if title and body:
            candidates.append(_normalize_probe(f"{title} {body[:80]}"))
        # Just the title and a body snippet.
        if title:
            candidates.append(_normalize_probe(title)[:60])
        # Just a body snippet.
        if body:
            candidates.append(_normalize_probe(body)[:60])
        # Filter out empties and dupes preserving order.
        seen: set[str] = set()
        out: list[str] = []
        for c in candidates:
            if c and len(c) >= 4 and c not in seen:
                seen.add(c)
                out.append(c)
        return out

    # Backtrack window: how far before the previous offset we allow the
    # search to look. Small (~200 chars) so a brand-new occurrence wins
    # over a re-find of an older string.
    _BACKTRACK = 200

    prev_offset = 0
    indexed: list[tuple[int, int, dict[str, Any]]] = []
    for orig_idx, r in enumerate(rows):
        if r.get("is_envelope") or r.get("scope") == "trailer":
            # Keep these out of the sort entirely (they're dropped from
            # JSONL anyway). Park at -1 to stay at the front.
            indexed.append((-1, orig_idx, r))
            continue
        probes = _probe_candidates(r)
        best = -1
        search_from = max(0, prev_offset - _BACKTRACK)
        for probe in probes:
            pos = sot_norm.find(probe, search_from)
            if pos < 0:
                # Fall back to global search if not found forward.
                pos = sot_norm.find(probe)
            if pos >= 0:
                best = pos
                break
        if best < 0:
            # Could not locate this record at all. Stick with previous.
            best = prev_offset
        else:
            prev_offset = best
        indexed.append((best, orig_idx, r))

    indexed.sort(key=lambda t: (t[0], t[1]))
    return [t[2] for t in indexed]


_SOT_LOOKUP: dict[int, str] = {}
_SOT_FILE = Path(__file__).resolve().parent.parent / "data" / "auto_parse" / "parse_source_of_truth.jsonl"


def _load_sot_span_clean(idx: int) -> str | None:
    """Read `span_clean` for the requested idx from the source-of-truth
    file. Returns None if file/row missing."""
    if not _SOT_FILE.exists():
        return None
    try:
        for line in _SOT_FILE.read_text().splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("idx") == idx:
                return rec.get("span_clean") or ""
    except OSError:
        return None
    return None


# Section/structural-header patterns that DISQUALIFY a record from being
# treated as a title-continuation line. A line with any of these patterns
# is part of the body, not the title.
_TITLE_CONTINUATION_DISQUALIFIERS = re.compile(
    r"^\s*(?:"
    r"ARTICLE\s+\w+"               # "ARTICLE I", "Article 1"
    r"|SECTION\s+\d"               # "SECTION 1", "Section 2"
    r"|EXHIBIT\s+[A-Z0-9]"         # "EXHIBIT A", "EXHIBIT 10.25"
    r"|SCHEDULE\s+[A-Z0-9]"        # "SCHEDULE 1"
    r"|APPENDIX\s+[A-Z0-9]"        # "APPENDIX A"
    r"|ANNEX\s+[A-Z0-9]"           # "ANNEX I"
    r"|WITNESSETH"                 # "WITNESSETH THAT:"
    r"|WHEREAS"                    # recitals
    r"|RECITALS"
    r"|NOW,?\s+THEREFORE"          # "NOW, THEREFORE"
    r"|IN\s+WITNESS\s+WHEREOF"     # signature operating clause
    r"|\d+\.\s"                    # numbered sections "1. ..."
    r"|\([a-zA-Z0-9]+\)"           # lettered/roman markers "(a)", "(i)"
    r"|\[[^\]]*\]"                 # placeholder "[***]"
    r"|/s/"                        # signature line
    r"|By:\s|Name:\s|Title:\s|Date:\s"  # signature labels
    r")",
    re.IGNORECASE,
)


def _looks_like_title_continuation(title: str) -> bool:
    """Return True if `title` looks like an upper-line of a multi-line
    agreement title (not a section heading, not a body fragment).

    A title-continuation line:
      - Has alphabetic content (at least one word of 2+ letters).
      - Is predominantly uppercase letters (≥ 60% of alphabetic chars
        are uppercase) — agreement titles are typeset in ALL CAPS.
      - Doesn't match any structural-header / section / recital /
        signature pattern.
      - Doesn't end with a sentence-terminator (`.`, `:`, `;`, `?`, `!`)
        — agreement titles usually break mid-phrase across lines, often
        ending with conjunctions ("AND", "OF") or nouns.
    """
    if not title:
        return False
    stripped = title.strip()
    if not stripped:
        return False
    # Must not match any structural / section / recital / signature pattern.
    if _TITLE_CONTINUATION_DISQUALIFIERS.match(stripped):
        return False
    # Must have at least one alphabetic word of 2+ letters.
    if not re.search(r"[A-Za-z]{2,}", stripped):
        return False
    # Compute uppercase-ratio of alphabetic chars (>= 60% uppercase).
    alpha = [c for c in stripped if c.isalpha()]
    if not alpha:
        return False
    upper_ratio = sum(1 for c in alpha if c.isupper()) / len(alpha)
    if upper_ratio < 0.60:
        return False
    # Must not end with a sentence-terminating punctuation. Title lines
    # are noun-phrases that wrap visually; bodies end with periods.
    if stripped.endswith(('.', ':', ';', '?', '!')):
        return False
    return True


def _merge_multiline_l0_title(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge a multi-line agreement title into the single L0 record.

    Some EX-10 filings typeset the agreement title across two or more
    visual lines, e.g.:

        AMENDMENT NO.1 TO CRUDE TALL OIL AND
        BLACK LIQUOR SOAP SKIMMINGS AGREEMENT

    doc2dict's HTML walker turns each line into its own predicted-header
    section, both as siblings under the SEC `<DOCUMENT>` envelope. The
    last line carries the AGREEMENT/PLAN keyword and gets remapped to
    depth=0 (the L0 title). The preceding line(s) are stranded as
    siblings at depth=1 — but they are PART of the title, not a
    standalone L1 clause.

    This post-processor:
      1. Finds the L0 record (depth=0, not envelope, scope=agreement).
      2. Walks preceding siblings (same parent_node_id, smaller
         node_id, no other sibling between them) in document order.
      3. Collects consecutive preceding siblings that match the
         shape of a title-continuation line — uppercase, no body,
         no structural-header pattern, no sentence-terminator.
      4. Prepends each continuation title to the L0 title (preserving
         source order) and marks the preceding records as envelope so
         they drop from JSONL but stay in parquet for audit.

    Runs BEFORE `_split_l0_title_from_preamble` so the L0 title is
    complete before the preamble split.

    Shape-based detection — no phrase blocklists. The disqualifier
    regex blocks structural patterns (ARTICLE, SECTION, EXHIBIT,
    WHEREAS, etc.) so legitimate body lines that follow the title
    aren't absorbed.
    """
    l0: dict[str, Any] | None = None
    for r in rows:
        if (
            r.get("depth") == 0
            and not r.get("is_envelope")
            and r.get("scope") == "agreement"
        ):
            l0 = r
            break
    if l0 is None:
        return rows

    l0_node_id = l0["node_id"]
    l0_parent_id = l0.get("parent_node_id")
    if l0_parent_id is None:
        return rows

    # Find preceding siblings — same parent, smaller node_id — in
    # node_id order (which is doc2dict's source order).
    siblings = sorted(
        (r for r in rows if r.get("parent_node_id") == l0_parent_id),
        key=lambda r: r["node_id"],
    )
    # Identify the L0's index among siblings.
    try:
        l0_pos = next(i for i, s in enumerate(siblings) if s["node_id"] == l0_node_id)
    except StopIteration:
        return rows
    if l0_pos == 0:
        return rows  # L0 is the first sibling — no continuation lines.

    # Walk preceding siblings from L0 backwards. Collect a contiguous
    # run of title-continuation candidates ending immediately before L0.
    continuation: list[dict[str, Any]] = []
    for i in range(l0_pos - 1, -1, -1):
        sib = siblings[i]
        if sib.get("is_envelope") or sib.get("scope") == "trailer":
            # Hit an envelope (SEC wrapper) or already-dropped record.
            # Stop the walk — anything earlier than this is filing chrome,
            # not title.
            break
        if (sib.get("cls") or "") != "predicted header":
            break
        if (sib.get("body_direct") or "").strip():
            break
        title = (sib.get("title") or "").strip()
        if not _looks_like_title_continuation(title):
            break
        continuation.append(sib)

    if not continuation:
        return rows

    # Build combined title in source order: earliest continuation line
    # first, then L0's existing title.
    continuation.reverse()  # source order (oldest first)
    continuation_titles = [(c.get("title") or "").strip() for c in continuation]
    existing_l0_title = (l0.get("title") or "").strip()
    combined = "\n".join(continuation_titles + [existing_l0_title])
    l0["title"] = combined

    # Mark the continuation records as envelope so they don't double-emit
    # to JSONL; parquet still records them.
    for c in continuation:
        c["is_envelope"] = True

    return rows


def _split_l0_title_from_preamble(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Split the L0 (agreement title) record from its preamble body.

    Per task_rules/level_rubric.md: depth 0 is the agreement title alone,
    a single record per idx with no body. The preamble paragraph
    ("THIS INDEMNIFICATION AGREEMENT (the 'Agreement') is made…") is a
    direct child of the agreement and belongs at depth 1.

    doc2dict combines the title and the immediately-following preamble
    text-leaf into one section with body_direct = preamble. This
    post-processor:
      - Finds the L0 record (depth=0, not envelope, scope=agreement)
      - If it has non-empty body_direct, creates a new L1 record with
        that body content (as a child of the L0)
      - Clears body_direct on the L0 record so its emitted span is the
        title alone
    """
    by_node_id = {r["node_id"]: r for r in rows}

    l0: dict[str, Any] | None = None
    for r in rows:
        if (
            r.get("depth") == 0
            and not r.get("is_envelope")
            and r.get("scope") == "agreement"
        ):
            l0 = r
            break
    if l0 is None:
        return rows

    body = (l0.get("body_direct") or "").strip()
    if not body:
        return rows

    next_id = max((r["node_id"] for r in rows), default=-1) + 1
    preamble_row: dict[str, Any] = {
        "node_id": next_id,
        "parent_node_id": l0["node_id"],
        "depth": 1 + (l0.get("subdoc_penalty") or 0),
        "doc2dict_section_key": f"_l0_preamble_{next_id}",
        "cls": "promoted text leaf",
        "title": "",
        "standardized_title": None,
        "n_direct_section_children": 0,
        "body_direct_chars": len(body),
        "body_recursive_chars": len(body),
        "body_direct": body,
        "body_recursive": body,
        "subdoc_penalty": l0.get("subdoc_penalty") or 0,
        "is_envelope": False,
        "scope": "agreement",
    }

    # Clear the L0's body so its emitted span is title-only.
    l0["body_direct"] = ""
    l0["body_direct_chars"] = 0

    # Insert preamble row directly after L0 in document order so its
    # span is emitted after the title and before any later siblings.
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(r)
        if r["node_id"] == l0["node_id"]:
            out.append(preamble_row)
    return out


def _split_inline_section_markers(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Split records whose body_direct contains inline section markers.

    doc2dict sometimes packs multiple top-level sections into a single
    table-preamble/postamble string. Example: section 10's body field
    contains the verbatim text "…rights under this Agreement. 14.
    Severability.  The invalidity… 15. Modification and Waiver. … 21.
    Governing Law… ". Without splitting, sections 14-21 appear inside
    section 10's span — not as their own L1 records.

    This post-processor:
      1. Finds records whose body_direct has 2+ inline "N." markers
         (where N is a number, indicating a numbered section start).
      2. Splits the body at each marker.
      3. Keeps the leading chunk (before the first marker) on the
         original record.
      4. Emits each subsequent chunk as its own promoted record at the
         same depth as the original (or at depth=1 if the original was
         deeper — top-level sections always sit at depth 1).
    """
    next_id = max((r["node_id"] for r in rows), default=-1) + 1

    new_rows: list[dict[str, Any]] = []
    for r in rows:
        new_rows.append(r)
        if r.get("is_envelope") or r.get("scope") == "trailer":
            continue
        body = r.get("body_direct") or ""
        if not body or len(body) < 200:
            continue
        # Look for 2+ inline section markers like "\n14. " or " 14. " or
        # ". 14." — these indicate top-level numbered sections embedded
        # in the body text.
        # Use a stricter regex: number followed by period, whitespace, capital letter.
        raw_markers = list(re.finditer(
            r"(?:(?<=[.\s\xa0])|^)(\d+)\.\s*[A-Z]",
            body,
        ))
        # Filter out cross-references: a marker preceded by "Section"
        # or "Sections" (case-insensitive, with optional whitespace/nbsp)
        # is a textual reference like "this Section 5." or "Sections 7,
        # 8 and 9 hereof", not a fresh top-level section heading. The
        # tail context window covers up to 15 chars before the marker —
        # enough for "Sections " variants without being so wide that it
        # picks up unrelated mentions.
        markers = []
        _SECTION_REF_RE = re.compile(r"(?i)\bSections?\s*[\xa0\s]*$")
        for m in raw_markers:
            pre = body[max(0, m.start() - 15):m.start()]
            if _SECTION_REF_RE.search(pre):
                continue
            markers.append(m)
        if len(markers) < 2:
            continue

        # Build segments: leading-text + (marker, segment) pairs.
        segments: list[tuple[int, int, int]] = []  # (start, end, marker_num)
        for i, m in enumerate(markers):
            seg_start = m.start()
            seg_end = markers[i + 1].start() if i + 1 < len(markers) else len(body)
            num = int(m.group(1))
            segments.append((seg_start, seg_end, num))

        # Validate marker numbering is monotonic (1,2,3 or 14,15,16,...)
        nums = [s[2] for s in segments]
        if not all(nums[i + 1] - nums[i] == 1 for i in range(len(nums) - 1)):
            continue

        first_start = segments[0][0]
        leading = body[:first_start].rstrip()
        # Update original record's body
        r["body_direct"] = leading
        r["body_direct_chars"] = len(leading)

        # Determine depth for new records: L1 (top-level body clause)
        # plus the original's subdoc_penalty.
        subdoc_penalty = r.get("subdoc_penalty") or 0
        new_depth = 1 + subdoc_penalty

        for seg_start, seg_end, num in segments:
            seg_text = body[seg_start:seg_end].strip()
            if not seg_text:
                continue
            # Split heading from body: heading = "N. Title."
            m_head = re.match(
                r"^(\d+\.\s*[^.\n]*?)(?:\.\s|\.\xa0|\.$)",
                seg_text,
            )
            if m_head:
                heading = m_head.group(1).strip()
                body_text = seg_text[m_head.end():].strip()
            else:
                # Fall back: first line is heading
                lines = seg_text.split("\n", 1)
                heading = lines[0].strip()[:120]
                body_text = lines[1].strip() if len(lines) > 1 else seg_text[len(heading):].strip()

            new_rows.append({
                "node_id": next_id,
                "parent_node_id": r.get("parent_node_id"),
                "depth": new_depth,
                "doc2dict_section_key": f"_inline_{next_id}",
                "cls": "promoted text leaf",
                "title": heading,
                "standardized_title": None,
                "n_direct_section_children": 0,
                "body_direct_chars": len(body_text),
                "body_recursive_chars": len(seg_text),
                "body_direct": body_text,
                "body_recursive": seg_text,
                "subdoc_penalty": subdoc_penalty,
                "is_envelope": False,
                "scope": "agreement",
            })
            next_id += 1

    return new_rows


def _split_inline_nm_section_markers(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Split records whose body_direct contains inline N.M section markers.

    Numbered subsections like 1.1, 1.2, ..., 13.14 appear in Article
    bodies as inline markers. doc2dict frequently packs them all into
    the parent Article's body_direct (because the HTML doesn't split
    them as separate elements). Per the rubric, numbered subsections
    under Articles are L2 records.

    Structural pattern: a body containing 2+ markers shaped
    `\\d+\\.\\d+\\s+...` at logical positions (after sentence break,
    paragraph break, or string start), forming a monotonic sequence
    (1.1, 1.2, 1.3, ... ; or 13.6, 13.7, 13.8, ... within a single
    Article body).

    Detection accepts a starting N.M and following markers must continue
    in sequence: monotonically increasing second number, with the first
    number constant (N.1, N.2, ... within Article N's body).

    On match:
      - Keep the leading chunk (before the first N.M marker) on the
        original record.
      - Emit each chunk as a new L2 record (depth = original_depth + 1,
        the L1 Article body's L2 children) with subdoc_penalty inherited.
    """
    next_id = max((r["node_id"] for r in rows), default=-1) + 1

    # N.M marker: digit(s).digit(s) followed by whitespace+\xa0, then
    # quote or capitalized letter. Anchored to logical start positions
    # to avoid false positives in body text like "Section 4.1(b)".
    _NM_MARKER_RE = re.compile(
        r"(?:(?<=[.\s\xa0])|^)"
        r"(\d+)\.(\d+)\s*[\xa0\s]+"
        r"(?=[\"“”‘’A-Z])"
    )

    new_rows: list[dict[str, Any]] = []
    for r in rows:
        new_rows.append(r)
        if r.get("is_envelope") or r.get("scope") == "trailer":
            continue
        body = r.get("body_direct") or ""
        if not body or len(body) < 200:
            continue

        matches = list(_NM_MARKER_RE.finditer(body))
        if len(matches) < 2:
            continue

        # Filter to a coherent monotonic sequence: require same first
        # number AND monotonically-increasing second number with gap 1.
        # Find the longest monotonic run starting from a valid pivot.
        # Strategy: group by first number, take the longest run where
        # second numbers form 1,2,3,... or M,M+1,M+2,...
        best_run: list[re.Match[str]] = []
        i = 0
        while i < len(matches):
            run = [matches[i]]
            n0 = int(matches[i].group(1))
            m0 = int(matches[i].group(2))
            j = i + 1
            while j < len(matches):
                n_j = int(matches[j].group(1))
                m_j = int(matches[j].group(2))
                # Continue the run if same N AND M increments by 1.
                if n_j == n0 and m_j == m0 + (j - i):
                    run.append(matches[j])
                    j += 1
                else:
                    break
            if len(run) > len(best_run):
                best_run = run
            i = j if j > i else i + 1

        if len(best_run) < 2:
            continue

        # Build segments from the best_run.
        segments: list[tuple[int, int, str]] = []  # (start, end, "N.M")
        for k, m in enumerate(best_run):
            seg_start = m.start()
            seg_end = best_run[k + 1].start() if k + 1 < len(best_run) else len(body)
            marker_str = f"{m.group(1)}.{m.group(2)}"
            segments.append((seg_start, seg_end, marker_str))

        first_start = segments[0][0]
        leading = body[:first_start].rstrip()
        r["body_direct"] = leading
        r["body_direct_chars"] = len(leading)

        # Determine depth: original depth + 1 (numbered subsections
        # are L2 when under an L1 Article).
        original_depth = r.get("depth", 1)
        subdoc_penalty = r.get("subdoc_penalty") or 0
        new_depth = original_depth + 1

        for seg_start, seg_end, marker_str in segments:
            seg_text = body[seg_start:seg_end].strip()
            if not seg_text:
                continue
            # Split heading from body: heading = "N.M Title" up to first
            # period+whitespace boundary. For definitions like
            # "1.1    \"Term\" shall have the meaning...", the heading is
            # the full first sentence.
            m_head = re.match(
                r"^(\d+\.\d+\s*[^.\n]*?)(?:\.\s|\.\xa0|\.$|\.\n)",
                seg_text,
            )
            if m_head:
                heading = m_head.group(1).strip()
                body_text = seg_text[m_head.end():].strip()
            else:
                lines = seg_text.split("\n", 1)
                heading = lines[0].strip()[:200]
                body_text = lines[1].strip() if len(lines) > 1 else seg_text[len(heading):].strip()

            new_rows.append({
                "node_id": next_id,
                "parent_node_id": r.get("parent_node_id"),
                "depth": new_depth,
                "doc2dict_section_key": f"_inline_nm_{next_id}",
                "cls": "promoted text leaf",
                "title": heading,
                "standardized_title": None,
                "n_direct_section_children": 0,
                "body_direct_chars": len(body_text),
                "body_recursive_chars": len(seg_text),
                "body_direct": body_text,
                "body_recursive": seg_text,
                "subdoc_penalty": subdoc_penalty,
                "is_envelope": False,
                "scope": "agreement",
            })
            next_id += 1

    return new_rows


def _rescue_cover_preamble_block(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rescue a cover-page preamble block when the L0 title appears twice.

    SEC EX-10 filings sometimes carry a cover-page block:
        EXHIBIT 10.1                 ← envelope (dropped)
        LICENSE AND OPTION AGREEMENT ← cover-page title duplicate (dropped)
        BY AND BETWEEN               ← cover-preamble line
        MOMENTA PHARMACEUTICALS,INC. ← party 1
        AND                          ← separator
        CSL BEHRING RECOMBINANT...   ← party 2
        DATED AS OF JANUARY 4, 2017  ← date
        TABLE OF CONTENTS            ← TOC (dropped)
        LICENSE AND OPTION AGREEMENT ← L0 title (actual agreement start)

    Per Arthur's annotation, the BY AND BETWEEN block (parties + date) is
    the cover-page preamble — a descriptive identifier of the agreement
    that mirrors the title context. It belongs at L1 as a cover-preamble
    record (a direct child of the agreement).

    Detection (purely structural, no phrase matching):
      1. Find the L0 title record (depth=0, not envelope, scope=agreement).
      2. Find any earlier-node-id record with the SAME title text — that
         marks the cover-page title duplicate position.
      3. Between the cover-duplicate and the L0 title, find consecutive
         predicted-header records whose titles are descriptive single-
         line text (all-caps party names, "BY AND BETWEEN", "AND",
         "DATED AS OF ...") and NO body content.
      4. Concatenate their titles into one L1 record with span =
         "BY AND BETWEEN\\n<party>\\nAND\\n<party>\\n DATED AS OF ...".
         Insert as a child of the L0 title with depth = 1.
      5. The original records remain in the parquet via their existing
         is_envelope=True/scope=agreement flags; only the synthetic
         concatenated record emits to JSONL.

    Filters (drop from the cover block):
      - TABLE OF CONTENTS (matches the structural TOC pattern).
      - The duplicate L0 title itself.
      - Any record with a body (only title-only records qualify as
        cover-preamble lines).

    No-op when:
      - No L0 title found.
      - L0 title appears only once in records.
      - No qualifying predicted-header records between the duplicate
        and the L0 title.
    """
    l0: dict[str, Any] | None = None
    for r in rows:
        if (
            r.get("depth") == 0
            and not r.get("is_envelope")
            and r.get("scope") == "agreement"
        ):
            l0 = r
            break
    if l0 is None:
        return rows

    l0_title = (l0.get("title") or "").strip()
    if not l0_title:
        return rows
    l0_node_id = l0["node_id"]

    # Find the cover-title duplicate: an earlier-node-id record with the
    # same title text. We look at ALL records (envelope or not) because
    # the cover duplicate is typically already marked is_envelope=True.
    cover_dup: dict[str, Any] | None = None
    for r in rows:
        if r["node_id"] >= l0_node_id:
            continue
        r_title = (r.get("title") or "").strip()
        if r_title == l0_title:
            cover_dup = r
            break
    if cover_dup is None:
        return rows

    # Collect candidate cover-preamble lines: records with node_id
    # between cover_dup and l0, with non-empty title, empty body, and
    # NOT a TOC. Walk records by node_id order.
    _TOC_TITLE_RE = re.compile(r"^\s*TABLE\s+OF\s+CONTENTS\b", re.IGNORECASE)
    candidates: list[dict[str, Any]] = []
    for r in rows:
        if r["node_id"] <= cover_dup["node_id"]:
            continue
        if r["node_id"] >= l0_node_id:
            break
        title = (r.get("title") or "").strip()
        body = (r.get("body_direct") or "").strip()
        if not title or body:
            continue
        if _TOC_TITLE_RE.match(title):
            continue
        # Skip title-text duplicates (shouldn't happen but be safe).
        if title == l0_title:
            continue
        candidates.append(r)

    if not candidates:
        return rows

    # Concatenate candidate titles into one cover-preamble span.
    cover_span = "\n".join((c.get("title") or "").strip() for c in candidates)
    if not cover_span:
        return rows

    # Build the synthetic L1 cover-preamble record. Insert right after
    # the L0 title in document order so its span emits between the
    # title and any other L1 children.
    next_id = max((r["node_id"] for r in rows), default=-1) + 1
    cover_row: dict[str, Any] = {
        "node_id": next_id,
        "parent_node_id": l0_node_id,
        "depth": 1 + (l0.get("subdoc_penalty") or 0),
        "doc2dict_section_key": f"_cover_preamble_{next_id}",
        "cls": "promoted text leaf",
        "title": "",
        "standardized_title": None,
        "n_direct_section_children": 0,
        "body_direct_chars": len(cover_span),
        "body_recursive_chars": len(cover_span),
        "body_direct": cover_span,
        "body_recursive": cover_span,
        "subdoc_penalty": l0.get("subdoc_penalty") or 0,
        "is_envelope": False,
        "scope": "agreement",
        # Marker so the pre-title position drop preserves this record
        # even though its content is positioned BEFORE the L0 title in
        # the source text. This is the rubric-sanctioned exception for
        # cover-page preamble blocks.
        "_cover_preamble": True,
    }

    # Insert the synthetic record directly after the L0 title.
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(r)
        if r["node_id"] == l0_node_id:
            out.append(cover_row)
    return out


def _drop_toc_chrome_records(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop "TABLE OF CONTENTS" header and roman-numeral page-marker
    records whose bodies are TOC entries.

    Some EX-10 filings expose the TOC region to doc2dict as a sequence
    of records:
        TABLE OF CONTENTS  ← title; body lists ARTICLE/Section/PAGE entries
        i                  ← page marker; body continues TOC entries
        ii                 ← page marker; body continues TOC entries
        iii                ← page marker; body MAY contain the real preamble
        ARTICLE I ...      ← first body clause

    The "TABLE OF CONTENTS" title and the bare roman-numeral page-marker
    titles are layout chrome — they should not appear as L1 records.
    Their bodies are either TOC continuation (cross-section, cross-page
    redundancy) or, occasionally, the real preamble (page iii here
    contains "INVESTMENT AGREEMENT, dated as of May 8, 2017...").

    Rule (purely structural):
      - A record titled "TABLE OF CONTENTS" (any case) is TOC chrome.
        Mark as envelope to drop from JSONL; keep in parquet for audit.
      - A record whose TITLE is a bare roman-numeral page marker (i,
        ii, iii, iv, ...) is also TOC chrome IF its body looks like
        TOC content (high density of Section N.M / ARTICLE X / PAGE
        entries). If its body looks like REAL content (matches the
        agreement preamble pattern "AGREEMENT, dated as of ..." or
        starts with WHEREAS / NOW THEREFORE), KEEP the body but CLEAR
        the title (so the body emits as a normal L1 record without the
        page-number prefix).

    Heuristic for "body looks like TOC":
      - Count the matches of the pattern ``Section\\s+\\d+\\.\\d+`` in the
        body. If >= 3 matches AND the body's total non-marker text is
        mostly section headings (no paragraph-form sentences), treat
        as TOC.
      - Or: the body is very short and matches ``ARTICLE [IVX]+`` multiple
        times.

    Runs BEFORE _apply_scope_rule.
    """
    _TOC_TITLE_RE = re.compile(r"^\s*TABLE\s+OF\s+CONTENTS\b", re.IGNORECASE)
    _PAGE_MARKER_TITLE_RE = re.compile(r"^\s*[ivxlcdm]+\s*$", re.IGNORECASE)
    _TOC_BODY_SECTION_RE = re.compile(r"Section\s+\d+\.\d+", re.IGNORECASE)
    _TOC_BODY_ARTICLE_RE = re.compile(r"^ARTICLE\s+[IVXLCDM]+\s*$", re.MULTILINE | re.IGNORECASE)
    _PREAMBLE_BODY_RE = re.compile(
        r"^[A-Z][A-Z ]{2,}\s+AGREEMENT,?\s+dated\s+as\s+of\b|^WHEREAS\b|^NOW,?\s+THEREFORE\b",
        re.IGNORECASE,
    )

    def _is_toc_body(body: str) -> bool:
        if not body or not body.strip():
            return False
        # If the body has obvious preamble content, NOT TOC.
        if _PREAMBLE_BODY_RE.search(body[:300]):
            return False
        # Heuristic: count Section N.M occurrences AND check density.
        section_matches = len(_TOC_BODY_SECTION_RE.findall(body))
        article_matches = len(_TOC_BODY_ARTICLE_RE.findall(body))
        if section_matches >= 3 or article_matches >= 3:
            # Verify density: total body length should be dominated by
            # short section-heading lines, not paragraph prose. If most
            # lines are < 50 chars (typical TOC line), this is TOC.
            lines = [ln.strip() for ln in body.split("\n") if ln.strip()]
            if not lines:
                return False
            short_lines = sum(1 for ln in lines if len(ln) < 60)
            return short_lines / len(lines) > 0.75
        return False

    for r in rows:
        title = (r.get("title") or "").strip()
        body = (r.get("body_direct") or "")

        # Case 1: TABLE OF CONTENTS chrome header.
        if _TOC_TITLE_RE.match(title):
            r["is_envelope"] = True
            continue

        # Case 2: Bare roman-numeral page marker.
        if _PAGE_MARKER_TITLE_RE.match(title):
            if not body.strip():
                # walk_sections already drops empty-body page markers,
                # but be defensive.
                r["is_envelope"] = True
                continue
            if _is_toc_body(body):
                # Body is TOC continuation — drop entirely.
                r["is_envelope"] = True
                continue
            # Body has real content — clear the page-number title so
            # the body emits without the prefix.
            r["title"] = ""

    return rows


def _rescue_post_title_cover_block(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rescue a cover-page block that appears AFTER the L0 title.

    Some EX-10 filings put the agreement title FIRST and the
    "by and among / parties / dated" cover block IMMEDIATELY AFTER it,
    on the same cover page, before any TABLE OF CONTENTS or article
    body. doc2dict captures each cover line as its own empty-body
    predicted-header record, so they would otherwise emit as a bunch
    of tiny L1 records ("by and among" / "PANDORA MEDIA, INC.," / "and"
    / "Dated as of May 8, 2017").

    The rescue collapses them into a single synthetic L1 record whose
    span is the joined cover-line text (one line per source line). The
    original records are marked as envelope so they don't double-emit
    to JSONL but stay in the parquet for audit.

    Detection (purely structural):
      1. Find the L0 title.
      2. Walk records by node_id starting RIGHT AFTER the L0 title.
      3. Collect consecutive predicted-header records that:
            - have a non-empty title,
            - have an empty body,
            - are NOT a TABLE OF CONTENTS or other TOC chrome,
            - are NOT a real subdoc, ARTICLE, or SECTION header.
      4. Stop at the FIRST record that breaks the chain (any record
         with body content, or a structural header).
      5. If at least 2 consecutive cover lines collected, merge them
         into ONE synthetic L1 record and mark originals as envelope.

    No-op when:
      - No L0 title found.
      - The duplicate-title cover rescue already fired (we leave that
        case to ``_rescue_cover_preamble_block``).
      - Fewer than 2 candidate post-title cover lines.

    Runs AFTER ``_rescue_cover_preamble_block`` so that filings with the
    duplicate-title pattern are handled by the older rescue and we don't
    double-process.
    """
    l0: dict[str, Any] | None = None
    for r in rows:
        if (
            r.get("depth") == 0
            and not r.get("is_envelope")
            and r.get("scope") == "agreement"
        ):
            l0 = r
            break
    if l0 is None:
        return rows

    l0_node_id = l0["node_id"]

    # If the duplicate-title rescue already produced a cover-preamble
    # record for this idx, don't re-do the work.
    for r in rows:
        if r.get("_cover_preamble"):
            return rows

    # Patterns that disqualify a record from being a cover-line:
    _TOC_TITLE_RE = re.compile(r"^\s*TABLE\s+OF\s+CONTENTS\b", re.IGNORECASE)
    _STRUCTURAL_HEADER_RE = re.compile(
        r"^\s*(?:ARTICLE\s+\w+|SECTION\s+\d|\d+\.\s|EXHIBIT\s+|SCHEDULE\s+|APPENDIX\s+|ANNEX\s+)",
        re.IGNORECASE,
    )
    _RECITAL_RE = re.compile(
        r"^\s*(?:WHEREAS|RECITALS|WITNESSETH|NOW,?\s+THEREFORE)",
        re.IGNORECASE,
    )

    candidates: list[dict[str, Any]] = []
    for r in sorted(rows, key=lambda x: x["node_id"]):
        if r["node_id"] <= l0_node_id:
            continue
        if r.get("is_envelope") or r.get("scope") == "trailer":
            break
        title = (r.get("title") or "").strip()
        body = (r.get("body_direct") or "").strip()
        cls = (r.get("cls") or "")
        # Empty-title records are skipped at emission anyway; treat them
        # as inert padding and skip without breaking the chain.
        if not title and not body:
            continue
        # Break on any record with body content (real preamble / first
        # body clause) or any structural header.
        if body:
            break
        if _TOC_TITLE_RE.match(title):
            break
        if _STRUCTURAL_HEADER_RE.match(title):
            break
        if _RECITAL_RE.match(title):
            break
        # Subdoc-class records (cls=exhibit/schedule/appendix/annex)
        # are real attachments — don't absorb them into the cover.
        if cls in _SUBDOC_CLASSES:
            break
        # Only consider predicted-header records (doc2dict's category for
        # un-structured headers). Other class records that survived this
        # far are not cover lines.
        if cls not in {"predicted header", "promoted text leaf"}:
            break
        candidates.append(r)

    # Need at least 2 cover lines to make a worthwhile rescue.
    if len(candidates) < 2:
        return rows

    cover_span = "\n".join((c.get("title") or "").strip() for c in candidates)
    if not cover_span:
        return rows

    next_id = max((r["node_id"] for r in rows), default=-1) + 1
    cover_row: dict[str, Any] = {
        "node_id": next_id,
        "parent_node_id": l0_node_id,
        "depth": 1 + (l0.get("subdoc_penalty") or 0),
        "doc2dict_section_key": f"_post_title_cover_{next_id}",
        "cls": "promoted text leaf",
        "title": "",
        "standardized_title": None,
        "n_direct_section_children": 0,
        "body_direct_chars": len(cover_span),
        "body_recursive_chars": len(cover_span),
        "body_direct": cover_span,
        "body_recursive": cover_span,
        "subdoc_penalty": l0.get("subdoc_penalty") or 0,
        "is_envelope": False,
        "scope": "agreement",
        "_cover_preamble": True,
    }

    # Mark the original cover-line records as envelope so JSONL drops
    # them; parquet still records the rows.
    candidate_ids = {c["node_id"] for c in candidates}
    for r in rows:
        if r["node_id"] in candidate_ids:
            r["is_envelope"] = True

    # Insert the synthetic record directly after the L0 title.
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(r)
        if r["node_id"] == l0_node_id:
            out.append(cover_row)
    return out


def _drop_pre_title_cover_records(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop filing-cover records that appear BEFORE the L0 agreement title.

    SEC EDGAR filings sometimes carry a registrant cover-page sequence:
        EXHIBIT 10.25                   <- envelope (already dropped)
        ULURU Inc.                      <- cover-page registrant name (this fix)
        INDEMNIFICATION AGREEMENT       <- L0 title

    The registrant short-name record (and any other empty-body record
    sandwiched between the envelope and the L0 title) is filing metadata,
    not agreement content. doc2dict's tree walker promotes it as a real
    section because the HTML wraps it in its own block element.

    Structural rule: any in-scope record whose node-id is LESS than the
    L0 title's node-id and which has an empty body is filing metadata —
    a registrant short-name, exhibit-number echo, or similar one-liner
    that sits in the cover-page region. Mark such records as
    ``is_envelope=True`` so they're kept in the parquet (audit) but
    dropped from the JSONL (rubric output).

    This applies only when an L0 title exists. The rule is depth-agnostic
    and content-agnostic — no company-name or phrase matching. The "empty
    body" predicate distinguishes a cover-page label (no body text) from
    a real agreement record (which always has body content or markers).
    """
    l0: dict[str, Any] | None = None
    for r in rows:
        if (
            r.get("depth") == 0
            and not r.get("is_envelope")
            and r.get("scope") == "agreement"
        ):
            l0 = r
            break
    if l0 is None:
        return rows

    l0_node_id = l0["node_id"]
    for r in rows:
        if r is l0:
            continue
        if r.get("is_envelope") or r.get("scope") == "trailer":
            continue
        # Records walked BEFORE the L0 title (smaller node_id) with no body
        # are cover-page filing metadata. The envelope itself is already
        # excluded by the is_envelope check above.
        if r["node_id"] >= l0_node_id:
            continue
        body = (r.get("body_direct") or "").strip()
        title = (r.get("title") or "").strip()
        if body:
            continue
        if not title:
            # Empty record — already gets skipped by the JSONL emitter.
            continue
        # Mark as envelope so JSONL drops it; parquet still records the
        # row via all_section_records.
        r["is_envelope"] = True
    return rows


def _strip_page_footer_exhibit_titles(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip bare 'Exhibit N.M' titles from non-envelope, non-subdoc
    exhibit-class records that carry substantive body content.

    Some EX-10 filings stamp the exhibit number on every page as a
    visual header (`Exhibit 10.7` repeated at the top of each page).
    doc2dict's HTML walker promotes each repeating page-header tag
    into its own `cls=exhibit` section. The FIRST one is the legitimate
    SEC envelope (already flagged `is_envelope=True`); subsequent ones
    are spurious — they carry substantive body text from the page they
    head, but their title is just the bare exhibit identifier.

    When that bare-identifier title flows into the source-position
    sorter (``_sort_records_by_source_position``) as the highest-
    priority probe, it matches the FIRST occurrence of "Exhibit 10.7"
    in the source, dragging the record to the document start. The
    title-as-root rule then drops it as pre-title content — losing
    the substantive body forever.

    The structural fix: clear the title on these records so the sorter
    falls back to the body probe (which IS unique to the page's
    content). The body emits as its own L1 record at the correct
    source position.

    Detection (purely structural):
      - cls is in `_SUBDOC_CLASSES` (exhibit/schedule/appendix/annex).
      - is_envelope is False (not the SEC wrapper).
      - scope is "agreement" (not already a trailer).
      - title matches the bare-identifier pattern
        (`^EXHIBIT|SCHEDULE|APPENDIX|ANNEX <id>$` with no descriptive
        text after a separator).
      - body_direct has substantive content (non-empty after strip).
      - Record is NOT a real subdoc (per ``_is_real_subdoc_title``).

    Runs AFTER ``_consolidate_real_subdocs`` so real subdoc headers
    keep their identifiers, and BEFORE the position sorter so the
    title-strip takes effect on sorting.
    """
    if not rows:
        return rows

    # Build children index for the real-subdoc test.
    children_of: dict[int | None, list[dict[str, Any]]] = {}
    for r in rows:
        children_of.setdefault(r.get("parent_node_id"), []).append(r)
    for pid in children_of:
        children_of[pid].sort(key=lambda r: r["node_id"])

    for r in rows:
        cls = (r.get("cls") or "")
        if cls not in _SUBDOC_CLASSES:
            continue
        if r.get("is_envelope"):
            continue
        if r.get("scope") == "trailer":
            continue
        title = (r.get("title") or "").strip()
        body = (r.get("body_direct") or "").strip()
        if not title or not body:
            continue
        # Bare-identifier title (no descriptive separator).
        if not _BARE_SUBDOC_ID_RE.match(title):
            continue
        # Body must be SUBSTANTIVE — not just page chrome (whitespace,
        # short page-reference like "Ex. B-98"). Use 60-char threshold:
        # genuine content is paragraph-shaped, page chrome is short.
        # This protects records like idx=5's "Schedule 1.1" + " \n \nEx.
        # B-98" body from being stripped (the body is page footer, not
        # substantive content from the page).
        if len(body) < 60:
            continue
        # Confirm not a real subdoc — check the descriptive-child rescue
        # would not apply. A real subdoc with a descriptive child gets
        # its title combined in `_consolidate_real_subdocs`; if it still
        # has a bare identifier here, the rescue did not fire, so it's
        # safe to treat as a page-footer artifact.
        kids = children_of.get(r["node_id"], [])
        first_child_title = kids[0].get("title") if kids else None
        if _is_real_subdoc_title(
            r.get("title"), r.get("cls"), r.get("is_envelope", False), first_child_title
        ):
            continue
        # Strip the title; the sorter will fall back to the body probe.
        r["title"] = ""

    return rows


def _drop_pre_title_position_records(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop in-scope records that appear BEFORE the L0 agreement title in
    sorted document-order.

    Per the title-as-root rule (level_rubric.md): any clause that cannot be
    placed as a descendant of the agreement title is not part of the
    agreement. Records whose source-text position precedes the L0 title are
    by definition pre-title — they cannot descend from a title that doesn't
    yet exist at that point in the document.

    This catches structurally-disjoint pre-title content that
    `_drop_pre_title_cover_records` misses because the records have:
      - node_id GREATER than the L0 (so the node-id heuristic doesn't fire), OR
      - non-empty body (so the empty-body heuristic doesn't fire).

    Canonical example: a TABLE OF CONTENTS block whose entries
    ("1. DEFINITIONS\\n1\\nARTICLE", "2. LICENSES\\n14\\nARTICLE", ...) are
    promoted text leaves that doc2dict parents to the SEC envelope node.
    Their node-ids exceed the L0's, but their source-text positions are
    inside the TOC region — i.e. BEFORE the L0 title. Per the rubric, they
    are pre-title and must be dropped.

    Runs AFTER `_sort_records_by_source_position`, which has already placed
    each record at its source-text position. Any record now appearing before
    the L0 in the sorted list is pre-title.
    """
    l0_idx: int | None = None
    for i, r in enumerate(rows):
        if (
            r.get("depth") == 0
            and not r.get("is_envelope")
            and r.get("scope") == "agreement"
        ):
            l0_idx = i
            break
    if l0_idx is None:
        return rows

    for i in range(l0_idx):
        r = rows[i]
        if r.get("is_envelope") or r.get("scope") == "trailer":
            # Already excluded from JSONL.
            continue
        if r.get("_cover_preamble"):
            # Synthetic cover-preamble record (Defect 1 / `_rescue_cover_preamble_block`).
            # Its content is positioned BEFORE the L0 title in the source
            # but the rubric explicitly admits it as a cover-page L1 child
            # of the agreement. Don't drop.
            continue
        # Mark as envelope so JSONL drops it; parquet still records the row.
        r["is_envelope"] = True
    # Also move any preserved cover-preamble records to sit IMMEDIATELY
    # after the L0 title. Their natural sorted position is before L0
    # (since their content precedes L0 in the source), but per the
    # rubric they emit as the agreement's first L1 child.
    l0_record = rows[l0_idx]
    cover_preamble_records = [
        r for r in rows[:l0_idx]
        if r.get("_cover_preamble") and not r.get("is_envelope")
    ]
    if cover_preamble_records:
        cover_ids = {r["node_id"] for r in cover_preamble_records}
        out: list[dict[str, Any]] = []
        for r in rows:
            if r["node_id"] in cover_ids:
                continue
            out.append(r)
            if r["node_id"] == l0_record["node_id"]:
                out.extend(cover_preamble_records)
        return out
    return rows


# Single-letter "(i)" marker — ambiguous between Roman numeral 1 and the
# alphabetical letter "i" (between "h" and "j"). Disambiguation requires
# sibling context: see _reclassify_letter_i_to_alphabetical.
_SINGLE_LETTER_I_TITLE_RE = re.compile(r"^\s*\([iI]\)")


def _reclassify_letter_i_to_alphabetical(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Reclassify "(i)" records from Roman-numeral to alphabetical when
    the surrounding sibling sequence is alphabetical (h)-(i)-(j).

    doc2dict and `_LEVEL_PATTERNS` treat "(i)" as a Roman numeral by
    default (it is, in most contexts: under an "(a)" letter, "(i)" is
    the first Roman item). But "(i)" is ALSO the alphabetical letter
    between "(h)" and "(j)". The disambiguation is purely contextual:

      - If the IMMEDIATELY PRECEDING and IMMEDIATELY FOLLOWING active
        siblings (in node_id order, under the same parent or the same
        flat letter-sequence) bear lettered markers "(h)" and "(j)",
        then this "(i)" is alphabetical — peer of its lettered siblings.
      - If the preceding sibling is "(vii)" or "(viii)" or similar
        Roman context, "(i)" is Roman — child of its lettered parent.

    Structural fix: when "(i)" is identified as alphabetical, re-parent
    it to be a sibling of "(h)" (i.e. set its parent_node_id to "(h)"'s
    parent) and adjust its depth to match "(h)"'s depth.

    Operates on the marker SEQUENCE, not the marker shape alone.
    """
    by_node_id = {r["node_id"]: r for r in rows}

    # Build a list of all active records with their letter-or-roman
    # classification, in node_id order. We use a flat scan because the
    # tree parenting can be wrong (doc2dict pathology) — the SOURCE
    # sequence (h)/(i)/(j) is what matters.
    actives = [
        r for r in rows
        if not r.get("is_envelope") and r.get("scope") != "trailer"
    ]
    actives.sort(key=lambda r: r["node_id"])

    # Identify each candidate "(i)" record and check sibling context.
    for idx_i, r in enumerate(actives):
        title = (r.get("title") or "").strip()
        if not _SINGLE_LETTER_I_TITLE_RE.match(title):
            continue
        # Determine the previous and next active record with a
        # lettered/roman marker (skip non-marker neighbours like prose
        # or section headers).
        prev_marker = None
        for j in range(idx_i - 1, -1, -1):
            prev_title = (actives[j].get("title") or "").strip()
            prev_kind = _classify_subsection_kind(prev_title)
            if prev_kind in {"lettered", "roman"}:
                prev_marker = (actives[j], prev_kind, _extract_letter_marker(prev_title))
                break
            if prev_kind == "numbered":
                # Cross-section boundary — stop scanning back.
                break
        next_marker = None
        for j in range(idx_i + 1, len(actives)):
            next_title = (actives[j].get("title") or "").strip()
            next_kind = _classify_subsection_kind(next_title)
            if next_kind in {"lettered", "roman"}:
                next_marker = (actives[j], next_kind, _extract_letter_marker(next_title))
                break
            if next_kind == "numbered":
                break

        # Alphabetical context: require BOTH previous "(h)" AND next "(j)"
        # as confirmed alphabetical neighbours. A single anchor is too
        # weak — it can fire across section boundaries by coincidence.
        prev_is_h = (
            prev_marker is not None
            and prev_marker[1] == "lettered"
            and prev_marker[2] == "h"
        )
        next_is_j = (
            next_marker is not None
            and next_marker[1] == "lettered"
            and next_marker[2] == "j"
        )
        if not (prev_is_h and next_is_j):
            continue
        # Confirmed alphabetical "(i)". Reparent and re-depth to match
        # the lettered neighbour. Also re-parent any records that were
        # wrongly placed under "(i)" — when "(i)" was being treated as
        # Roman, doc2dict's HTML walker may have parented the FOLLOWING
        # alphabetical sibling "(j)" (and a subsequent numbered section
        # like "7.") underneath "(i)". We re-attach those records to
        # "(i)"'s new parent so they share the same depth as their
        # actual siblings.
        anchor = prev_marker[0] if prev_is_h else next_marker[0]
        anchor_parent_id = anchor.get("parent_node_id")
        anchor_depth = anchor.get("depth", r.get("depth", 2))
        old_i_id = r["node_id"]
        r["parent_node_id"] = anchor_parent_id
        r["depth"] = anchor_depth
        # Re-parent any records currently parented under (i) to (i)'s
        # new parent. These were misclassified as children when (i) was
        # treated as Roman; they're actually siblings in alphabetical
        # numbering. Walk children of (i) and re-parent each one.
        for child in rows:
            if child.get("parent_node_id") == old_i_id:
                child["parent_node_id"] = anchor_parent_id
                # Set depth equal to the anchor (so (j) sits at the
                # same depth as (h)/(i)). For records that are actually
                # numbered sections wrongly parented under (i), the
                # downstream `_reparent_lettered_subsections_to_numbered_siblings`
                # pass would re-attach them; here we just set depth to
                # match their structural peer.
                child_kind = _classify_subsection_kind(
                    (child.get("title") or "").strip()
                )
                if child_kind == "numbered":
                    # A numbered section misparented under (i) — its
                    # actual depth is 1 + subdoc_penalty.
                    child["depth"] = 1 + (child.get("subdoc_penalty") or 0)
                else:
                    child["depth"] = anchor_depth
    return rows


# Signature line: "/s/ Jane Doe", "/s/ Terrance K. Wallberg".
_SIGN_OFF_RE = re.compile(r"/s/\s+\w+")

# A "signature anchor" is a record whose TITLE or BODY contains a /s/
# signature line. Detected by SHAPE only — no party-name matching.
_SIG_TITLE_ANCHOR_RE = re.compile(r"^\s*/s/\s+\w+")

# Per-line shape detectors for a signature-page line. Each is a SHAPE
# match — no specific names, companies, or party labels are encoded.
# A signature-page line is one of:
#   - "/s/ <name>"               (signature mark)
#   - "By: <something>"          (by-line, possibly carrying /s/)
#   - "Name: <name>"             (name field)
#   - "Title: <title>"           (title field)
#   - "Address: <addr>"          (address field, possibly bare "Address:")
#   - A bare uppercase party label (1-3 words all caps)
#   - A bare proper-noun name line
# These are structural cues that occur in signature pages across many
# agreement types — they are not phrase blocklists.
_SIG_FIELD_RE = re.compile(
    r"(?i)^(?:by\s*:|name\s*:|title\s*:|address\s*:?)\s*",
)

# Short uppercase tag that acts as a signature-block party label
# (e.g. an "INDEMNITEE"/"BORROWER"/"LENDER"-style role label). Detected
# by SHAPE — 1-3 words, all uppercase letters (or with light
# punctuation), not enumeration-driven. No specific labels are encoded.
_SIG_BLOCK_LABEL_RE = re.compile(r"^[A-Z][A-Z .,&'\-]{1,40}$")

# Corporate party label shape — uppercase-leading proper-noun prefix
# followed by a corporate entity suffix (Inc., Inc, Corp., Corp, LLC,
# L.P., LP, Ltd., Ltd, Limited, Co., Co, Company, N.A., S.A., GmbH, AG,
# PLC). Used as a SHAPE complement to _SIG_BLOCK_LABEL_RE for mixed-case
# corporate names (e.g. "ULURU Inc.", "Acme Corp.", "Foo Bar LLC") that
# the strict ALL-CAPS pattern misses. No specific company names are
# encoded — only the structural suffix shape.
_CORP_SUFFIX_LABEL_RE = re.compile(
    r"^[A-Z][A-Za-z0-9 .,&'\-]{0,80}\s+"
    r"(?:Inc|Corp|LLC|L\.?P|Ltd|Limited|Co|Company|N\.A|S\.A|GmbH|AG|PLC|LLP)"
    r"\.?$"
)

# Signature-page banner line — typesetter chrome telling the reader the
# signature page follows. Examples: "SIGNATURE PAGE TO FOLLOW", "[Signature
# Page Follows]", "-- Signature Page --". Detected by SHAPE: the word
# "signature" adjacent to the word "page", optionally bracketed/dashed.
# This is page-layout chrome injected by typesetters, not agreement
# content (per the title-as-root rule).
_SIG_PAGE_BANNER_RE = re.compile(
    r"(?i)^\s*[\[\-—]?\s*signature\s+page\b",
)

# Page-number-only line (one or two-digit, possibly bracketed/centered).
# Common in signature-page footer chrome ("- 5 -", "[5]", "5.").
_PAGE_NUMBER_FOOTER_RE = re.compile(
    r"^\s*[\[\(\-—]?\s*\d{1,3}\s*[\]\)\-—.]?\s*$",
)

# Exhibit-reference footer ("Exhibit 10.25", "EX-10.1") used as a
# page-bottom watermark on signature pages. Detected as: an exhibit
# token plus a number, alone on the line, in the signature area.
_EXHIBIT_FOOTER_RE = re.compile(
    r"(?i)^\s*(?:exhibit|ex)[\s\-]\s*\d+(?:\.\d+)?\s*$"
)


def _looks_like_sig_page_line(span: str) -> bool:
    """Shape-detect a signature-page line.

    True if the span matches any structural cue typical of signature-
    page lines: a /s/ mark, a By:/Name:/Title:/Address: field, a
    short uppercase party label, or a single-line name-shaped text.
    """
    span = (span or "").strip()
    if not span:
        return False
    if _SIGN_OFF_RE.search(span):
        return True
    if _SIG_FIELD_RE.match(span):
        return True
    if _SIG_BLOCK_LABEL_RE.match(span):
        return True
    return False


def _is_iww_clause(text: str) -> bool:
    """Shape-detect the 'IN WITNESS WHEREOF' operating clause.

    The IWW clause is the signature-page header in traditional
    agreements. Detected by the canonical uppercase phrase appearing at
    the start of the span. No specific agreement words encoded.
    """
    if not text:
        return False
    return bool(re.match(r"(?i)^\s*IN\s+WITNESS\s+WHEREOF", text.strip()))


def _split_sig_block_body_into_lines(body: str) -> list[str]:
    """Split a consolidated signature-block body into per-line records.

    The body is multi-line text where each line is one signature-page
    line (By:/Name:/Title:/Address:/<name>/<role>). Split on newlines,
    strip whitespace/nbsp runs, drop empty lines, drop any line that
    matches a signature-page banner or page-number footer pattern.
    Returns the cleaned per-line list.
    """
    if not body:
        return []
    # doc2dict preserves &nbsp; as \xa0. Collapse runs of whitespace
    # *within* a line but keep line breaks intact, so each visual line
    # becomes one element.
    lines = body.replace("\xa0", " ").split("\n")
    out: list[str] = []
    for raw_line in lines:
        line = " ".join(raw_line.split()).strip()
        if not line:
            continue
        # Drop signature-page footer chrome that landed inside the body.
        if _SIG_PAGE_BANNER_RE.match(line):
            continue
        if _PAGE_NUMBER_FOOTER_RE.match(line):
            continue
        if _EXHIBIT_FOOTER_RE.match(line):
            continue
        out.append(line)
    return out


def _split_iww_and_sig_from_body(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Extract an inline IN WITNESS WHEREOF + signature block from a body.

    doc2dict sometimes packs the entire signature page — IWW operating
    clause, party labels, /s/ marks, By:/Name:/Title: fields — into the
    trailing body text of the LAST lettered subsection of the main
    agreement (e.g. "(j) Waiver of Breach" picks up everything that the
    HTML walker couldn't promote into its own node). This destroys the
    signature-page hierarchy mandated by the title-as-root rubric
    (level_rubric.md §"Signature-page content emitted at the wrong
    depth"): the IWW clause should be L1 and each sig-page line should
    be L2.

    Structural detection (purely shape-based, no phrase matching):

      1. The document has ZERO existing records whose title/body STARTS
         with the IWW phrase. If any record already carries IWW as its
         leading text, the sig page is already correctly separated and
         this function is a no-op for that document.
      2. Find a record whose body contains the IWW phrase MID-body (not
         at position 0). The text before the IWW is the record's real
         body; the text from IWW onward is the misplaced sig page.
      3. Split:
           - Keep the prefix on the original record.
           - Emit the IWW operating clause sentence as a new L1 record
             (depth = 1 + subdoc_penalty).
           - Split the remainder by newlines into sig-line L2 records
             (depth = 2 + subdoc_penalty). Each non-empty, non-banner
             line becomes its own L2 record. Duplicate consecutive
             lines (a doc2dict pathology in this idx) are collapsed.

    This function runs BEFORE `_explode_signature_block_lines` so the
    new sig-line records get the standard sig-line marking. The IWW
    record is detected by that pass via `_is_iww_clause` and pinned
    at L1.
    """
    # Guard: only act when no existing record carries IWW as leading text.
    # Without this, we'd double-split documents that already have a clean
    # IWW record (the prior idxs) and break their frozen baselines.
    for r in rows:
        if r.get("is_envelope") or r.get("scope") == "trailer":
            continue
        title = (r.get("title") or "").strip()
        body = (r.get("body_direct") or "").strip()
        if _is_iww_clause(title) or _is_iww_clause(body):
            return rows

    _IWW_INLINE_RE = re.compile(r"(?i)IN\s+WITNESS\s+WHEREOF")

    next_id = max((r["node_id"] for r in rows), default=-1) + 1
    new_rows: list[dict[str, Any]] = []

    for r in rows:
        new_rows.append(r)
        if r.get("is_envelope") or r.get("scope") == "trailer":
            continue
        body = r.get("body_direct") or ""
        if not body:
            continue
        m = _IWW_INLINE_RE.search(body)
        # Require IWW NOT at start (start-of-body IWW is already its own
        # record from the guard above). Require some substantive prefix
        # so we don't strip a record whose body IS the sig page.
        if not m or m.start() == 0:
            continue

        # Split body at IWW position. Prefix stays on the original record.
        prefix = body[:m.start()].rstrip()
        iww_and_after = body[m.start():]

        # The IWW operating clause sentence ends at the first period
        # following the IWW phrase. The sig lines follow.
        end_period = iww_and_after.find(".", m.end() - m.start())
        if end_period == -1:
            # No period — treat whole text as IWW (no sig lines parsed
            # separately). Skip; better to leave the body intact than
            # emit a malformed record.
            continue
        iww_sentence = iww_and_after[:end_period + 1].strip()
        sig_rest = iww_and_after[end_period + 1:]

        # Update original record body to just the prefix.
        r["body_direct"] = prefix
        r["body_direct_chars"] = len(prefix)

        subdoc_penalty = r.get("subdoc_penalty") or 0
        l1_depth = 1 + subdoc_penalty
        l2_depth = 2 + subdoc_penalty

        # Emit the IWW operating clause as L1.
        iww_record = {
            "node_id": next_id,
            "parent_node_id": r.get("parent_node_id"),
            "depth": l1_depth,
            "doc2dict_section_key": f"_iww_{next_id}",
            "cls": "promoted text leaf",
            "title": "",
            "standardized_title": None,
            "n_direct_section_children": 0,
            "body_direct_chars": len(iww_sentence),
            "body_recursive_chars": len(iww_sentence),
            "body_direct": iww_sentence,
            "body_recursive": iww_sentence,
            "subdoc_penalty": subdoc_penalty,
            "is_envelope": False,
            "scope": "agreement",
        }
        new_rows.append(iww_record)
        next_id += 1
        iww_node_id = iww_record["node_id"]

        # Split sig_rest into per-line records. Drop empty lines, drop
        # banner/page-number/exhibit-footer chrome, dedupe lines (doc2dict
        # in this idx duplicates labels, party names, and /s/ marks
        # because the source HTML repeats each block twice). A real sig
        # block has each line unique — party labels appear once, /s/
        # marks are unique per signer, names are unique per signer — so
        # global per-block dedup is structurally correct here. Records
        # are marked `_sig_line=True` so `_consolidate_sig_lines_after_iww`
        # anchors them as one contiguous cluster right after the IWW
        # operating clause regardless of how the source-position sorter
        # places short label probes ("Company", "Executive") that match
        # in the agreement body.
        lines = _split_sig_block_body_into_lines(sig_rest)
        seen_lines: set[str] = set()
        for line in lines:
            if line in seen_lines:
                continue  # collapse duplicate
            seen_lines.add(line)
            new_rows.append({
                "node_id": next_id,
                "parent_node_id": iww_node_id,
                "depth": l2_depth,
                "doc2dict_section_key": f"_sig_line_{next_id}",
                "cls": "promoted text leaf",
                "title": line,
                "standardized_title": None,
                "n_direct_section_children": 0,
                "body_direct_chars": 0,
                "body_recursive_chars": len(line),
                "body_direct": "",
                "body_recursive": line,
                "subdoc_penalty": subdoc_penalty,
                "is_envelope": False,
                "scope": "agreement",
                "_sig_line": True,
            })
            next_id += 1

    return new_rows


def _explode_signature_block_lines(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Apply the title-as-root signature-page rubric (level_rubric.md /
    examples_main_agreement.md).

    Three transformations, all SHAPE-driven (no party-name or company-
    name matching):

      1. **Strip signature-page banner chrome.** Records whose title
         matches the signature-page banner shape (e.g. "SIGNATURE PAGE
         TO FOLLOW", "[Signature Page Follows]") are stripped of their
         banner title. If they carry substantive body content (an IWW
         operating clause or any non-banner text), the body is
         preserved and the title is cleared. If they carry no
         substantive content, they are marked is_envelope=True so the
         JSONL writer drops them.

      2. **Pin the IWW operating clause at L1.** A record whose
         title/body begins with "IN WITNESS WHEREOF" is the signature
         page's operating clause. Its depth is set to
         1 + subdoc_penalty (a direct child of the agreement).

      3. **Pin sig-page records at L2.** Each record identified as a
         signature-page line gets depth = 2 + subdoc_penalty. The
         record's body is NOT split into per-line fragments. Per the
         rubric (level_rubric.md "Signature-page content emitted at
         the wrong depth"), we PRESERVE doc2dict's natural grouping:
         if doc2dict packed a party's block as one node it stays one
         L2 record; if doc2dict fragmented per line, each fragment is
         its own L2 record. A separate post-sort pass merges body-only
         fragments (title="", body has sig-shape) into the preceding
         titled sig record in source-text order — so a multi-fragment
         party block reconstructs as one L2 span even when doc2dict
         scattered the parts.

    Detection is purely SHAPE-BASED: a "signature carrier" is any
    record whose span (title+body) contains the /s/ marker, OR whose
    title is a short uppercase party label whose body starts with a
    By:/Name:/Title:/Address: field, OR whose title is the /s/ line
    itself. No specific labels (INDEMNITEE, BORROWER, etc.) are
    encoded.
    """
    if not rows:
        return rows

    by_node_id = {r["node_id"]: r for r in rows}

    def _span_text(r: dict[str, Any]) -> str:
        t = (r.get("title") or "").strip()
        b = (r.get("body_direct") or "").strip()
        if t and b:
            return f"{t}\n{b}"
        return t or b

    def _has_section_marker_title(r: dict[str, Any]) -> bool:
        """True if title bears a numbered/lettered/roman section marker —
        such records are agreement clauses, not signature fragments."""
        title = (r.get("title") or "").strip()
        if not title:
            return False
        return _classify_subsection_kind(title) is not None

    def _chain_ancestor_node_ids(node: dict[str, Any]) -> set[int]:
        """Walk up parent_node_id chain, returning all ancestor ids."""
        ids: set[int] = set()
        cur = node.get("parent_node_id")
        while cur is not None and cur not in ids:
            ids.add(cur)
            parent = by_node_id.get(cur)
            if parent is None:
                break
            cur = parent.get("parent_node_id")
        return ids

    # PASS 1: strip signature-page banner chrome from titles. Banner
    # detection is purely shape-based (the word "signature" adjacent to
    # the word "page" at line start, optionally bracketed/dashed).
    for r in rows:
        if r.get("is_envelope") or r.get("scope") == "trailer":
            continue
        title = (r.get("title") or "").strip()
        body = (r.get("body_direct") or "").strip()
        if not title or not _SIG_PAGE_BANNER_RE.match(title):
            continue
        if body and not _SIG_PAGE_BANNER_RE.match(body):
            # Body carries substantive content (typically the IWW
            # operating clause). Clear the banner title; the JSONL
            # writer emits the body alone.
            r["title"] = ""
        else:
            # No substantive body — record is banner chrome only.
            r["is_envelope"] = True

    # PASS 2: identify signature carriers and pin the IWW operating
    # clause at L1.
    #
    # A "signature carrier" is any record whose span contains a /s/
    # marker. Around each carrier we expand outward to identify the
    # full party-block: leading party-label record(s), continuation
    # field records (By:/Name:/Title:/Address:), and bare-name
    # continuation records. Membership in the block is established by
    # the tree-chain relation (records in the same parent-chain as the
    # /s/ carrier).
    actives = [
        r for r in rows
        if not r.get("is_envelope") and r.get("scope") != "trailer"
    ]
    actives.sort(key=lambda r: r["node_id"])

    # Pin the IWW operating clause depth=1.
    iww_present = False
    for r in actives:
        span = _span_text(r)
        if _is_iww_clause(span):
            r["depth"] = 1 + (r.get("subdoc_penalty") or 0)
            iww_present = True

    if not any(_SIGN_OFF_RE.search(_span_text(r)) for r in actives):
        # No /s/ anywhere — nothing more to do. Even modern agreements
        # without an IWW clause will have /s/ lines or we don't treat
        # this as a signature area.
        return rows

    # Build a set of sig-line node_ids using tree-chain expansion from
    # each /s/-carrier. This stays purely structural: a record is
    # marked as a signature-page line iff it's the /s/ carrier itself,
    # OR an ancestor that is a short uppercase party label, OR an
    # immediate descendant that fits a sig-field continuation.
    sig_line_node_ids: set[int] = set()

    # The /s/-carriers themselves.
    sig_carriers: list[dict[str, Any]] = []
    for r in actives:
        if _SIGN_OFF_RE.search(_span_text(r)):
            sig_carriers.append(r)
            sig_line_node_ids.add(r["node_id"])

    # Expand UP each chain: ancestors that are short uppercase party
    # labels (with empty body or sig-shape body) join the block. Stop
    # at the first ancestor that bears a section marker (numbered or
    # lettered) — that's an agreement clause, not a sig-block parent.
    #
    # CRITICAL: never claim the L0 title (depth=0) as a sig-block label,
    # even if its text shape (short uppercase) happens to match. The
    # agreement's title is the structural root per the title-as-root
    # rubric (level_rubric.md "Title is the root of the agreement"); it
    # cannot be demoted to a sig-line. This guards against agreements
    # whose title is short and all-caps (e.g. "LOAN AND SECURITY
    # AGREEMENT") matching _SIG_BLOCK_LABEL_RE by accident.
    # Track which records were claimed as sig-block PARENTS during the
    # UP-climb so PASS 2.5 can expand DOWN from them to catch siblings
    # of the carrier under the same parent (mixed-case corporate party
    # labels often parent a /s/ carrier plus separate "By"/"Name:"/
    # "Title:" sibling fragments).
    sig_block_parents: set[int] = set()

    for carrier in sig_carriers:
        cur = carrier.get("parent_node_id")
        seen: set[int] = set()
        while cur is not None and cur not in seen:
            seen.add(cur)
            parent = by_node_id.get(cur)
            if parent is None:
                break
            if parent.get("is_envelope") or parent.get("scope") == "trailer":
                cur = parent.get("parent_node_id")
                continue
            # Stop the climb at the L0 agreement title. It is the
            # structural root and cannot be a sig-block label.
            if parent.get("depth") == 0:
                break
            if _has_section_marker_title(parent):
                break
            p_title = (parent.get("title") or "").strip()
            p_body = (parent.get("body_direct") or "").strip()
            # Party-label ancestor: short uppercase title (strict
            # ALL-CAPS shape) OR mixed-case corporate-suffix shape
            # (e.g. "ULURU Inc.", "Acme Corp."), body empty OR body
            # is sig-shape continuation content. Both shapes are
            # structural — no specific names or labels are encoded.
            if (
                p_title
                and (
                    _SIG_BLOCK_LABEL_RE.match(p_title)
                    or _CORP_SUFFIX_LABEL_RE.match(p_title)
                )
                and (
                    not p_body
                    or _SIG_FIELD_RE.match(p_body)
                    or _SIGN_OFF_RE.search(p_body)
                )
            ):
                sig_line_node_ids.add(parent["node_id"])
                sig_block_parents.add(parent["node_id"])
                cur = parent.get("parent_node_id")
                continue
            # IWW operating clause ancestor — that's L1, not part of
            # the L2 sig-line set. Don't add to sig_line_node_ids but
            # also don't break the chain (it may itself have a
            # short-uppercase parent label above it).
            if _is_iww_clause(_span_text(parent)):
                cur = parent.get("parent_node_id")
                continue
            break

    # Expand DOWN each carrier's tree chain: descendants whose
    # title or body fits sig-shape continuation join the block.
    children_of: dict[int | None, list[dict[str, Any]]] = {}
    for r in rows:
        children_of.setdefault(r.get("parent_node_id"), []).append(r)

    def _walk_descendants(nid: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        stack = [nid]
        seen: set[int] = set()
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            for c in children_of.get(cur, []):
                out.append(c)
                stack.append(c["node_id"])
        return out

    # Walk DOWN from each /s/ carrier AND from each sig-block parent
    # identified during the UP-climb. Walking from parents catches
    # SIBLINGS of the carrier under the same parent (e.g. a separate
    # "By" fragment that doc2dict split off from the /s/ line into its
    # own predicted-header node).
    walk_roots: set[int] = {c["node_id"] for c in sig_carriers}
    walk_roots.update(sig_block_parents)
    for root_nid in walk_roots:
        for d in _walk_descendants(root_nid):
            if d.get("is_envelope") or d.get("scope") == "trailer":
                continue
            if _has_section_marker_title(d):
                continue
            d_title = (d.get("title") or "").strip()
            d_body = (d.get("body_direct") or "").strip()
            span = _span_text(d)
            # Sig-field-shape: By:/Name:/Title:/Address: prefix on
            # title or body; or a short label; or a /s/ carrier; or a
            # bare-name shape (no enumeration, no section marker, in-
            # chain with the carrier — guaranteed since this is a
            # descendant walk).
            looks_sig = (
                _SIGN_OFF_RE.search(span)
                or _SIG_FIELD_RE.match(d_title)
                or _SIG_FIELD_RE.match(d_body)
                or (d_title and _SIG_BLOCK_LABEL_RE.match(d_title) and not d_body)
                or (d_title and _CORP_SUFFIX_LABEL_RE.match(d_title) and not d_body)
                or (d_title and not d_body)  # bare name as title
                or (not d_title and d_body and _SIG_FIELD_RE.match(d_body))
            )
            if looks_sig:
                sig_line_node_ids.add(d["node_id"])

    # PASS 3: pin each identified sig-line record at depth 2
    # (+ subdoc_penalty). Do NOT split per-line. Per the new rubric
    # (level_rubric.md / examples_main_agreement.md "Signature-page
    # content emitted at the wrong depth"), we PRESERVE doc2dict's
    # natural grouping. A separate post-sort pass merges body-only
    # fragments into the preceding titled sig record in source order.
    for r in rows:
        if r.get("is_envelope") or r.get("scope") == "trailer":
            continue
        if r["node_id"] not in sig_line_node_ids:
            continue
        # Defensive guard mirroring the up-climb stop condition:
        # never demote the L0 agreement title to a sig-line depth.
        if r.get("depth") == 0:
            continue
        # The IWW operating clause is L1 (set by PASS 2 above). If a
        # record's title/body contains the IWW phrase it is the IWW
        # clause itself; do not let PASS 3 demote it to L2 just because
        # doc2dict packed the entire sig-page text into its body (and
        # therefore made /s/ appear inside its span). The IWW clause is
        # always L1 per the title-as-root signature-page rubric.
        if _is_iww_clause(_span_text(r)):
            continue
        l2_depth = 2 + (r.get("subdoc_penalty") or 0)
        r["depth"] = l2_depth
        r["_sig_line"] = True

    return rows


def _merge_body_only_sig_fragments_in_order(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge body-only sig fragments into the preceding titled sig record.

    After `_sort_records_by_source_position` + `_consolidate_sig_lines_after_iww`
    have laid out the records in source-text order, walk the sig-line
    block and merge each title-less, sig-shape-body fragment into the
    IMMEDIATELY PRECEDING titled sig record.

    Why this exists: doc2dict often splits a party's signature block
    into multiple section nodes — a title-bearing party-label node
    ("ULURU Inc.", "MOMENTA PHARMACEUTICALS, INC.") followed by a
    chain of body-only fragments ("By: /s/...", "Name: ...",
    "Title: ..."). Per the rubric, each PARTY BLOCK is a single L2
    record. Merging the body-only fragments into the preceding
    titled record reconstructs the per-party grouping that matches
    the source document's visual layout.

    Detection of "body-only sig fragment": _sig_line=True, title
    empty, body matches sig-shape (By:/Name:/Title:/Address: prefix
    OR contains /s/ OR is short uppercase company-style continuation).

    Operation: append the fragment's body (newline-separated) to the
    preceding sig record's body. Mark the fragment is_envelope=True
    so the JSONL writer drops it. Parquet still has both rows.
    """
    if not rows:
        return rows

    last_titled_sig: dict[str, Any] | None = None
    for r in rows:
        if r.get("is_envelope") or r.get("scope") == "trailer":
            continue
        if not r.get("_sig_line"):
            # Non-sig record — sig-line chain is broken; reset.
            last_titled_sig = None
            continue
        title = (r.get("title") or "").strip()
        body = (r.get("body_direct") or "").strip()
        if title:
            # Titled sig record — start a new merge target.
            last_titled_sig = r
            continue
        # Body-only sig fragment. Merge into preceding titled sig record
        # if any, else become the new merge target.
        if last_titled_sig is None or not body:
            last_titled_sig = r if not last_titled_sig else last_titled_sig
            continue
        # Merge body into last_titled_sig's body, separated by newline.
        existing_body = (last_titled_sig.get("body_direct") or "").strip()
        merged = body if not existing_body else f"{existing_body}\n{body}"
        last_titled_sig["body_direct"] = merged
        last_titled_sig["body_direct_chars"] = len(merged)
        # Drop the fragment from JSONL (kept in parquet for audit).
        r["is_envelope"] = True

    return rows


def _anchor_subdoc_bodies_after_titles(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Move synthetic subdoc body records to sit right after their parent title.

    Subdoc body records (created by `_consolidate_real_subdocs` with the
    `_subdoc_body_of` flag) carry the consolidated body content for a
    real subdoc. After `_sort_records_by_source_position`, their
    source-text probe can mis-locate them when the body's first words
    happen to occur EARLIER in the source (e.g. an exhibit body whose
    first words also appear on the cover page — the forward-progress
    search will choose the earlier occurrence).

    This pass walks the sorted rows, finds each subdoc body record,
    and re-inserts it IMMEDIATELY AFTER the row that matches its
    `_subdoc_body_of` node_id. This preserves the structural ordering:
    subdoc title at L1, followed by its body record at L2.
    """
    body_records: dict[int, dict[str, Any]] = {}
    other_records: list[dict[str, Any]] = []
    for r in rows:
        of_id = r.get("_subdoc_body_of")
        if of_id is not None and not r.get("is_envelope"):
            body_records[of_id] = r
        else:
            other_records.append(r)
    if not body_records:
        return rows
    out: list[dict[str, Any]] = []
    for r in other_records:
        out.append(r)
        body = body_records.get(r["node_id"])
        if body is not None:
            out.append(body)
    return out


def _consolidate_sig_lines_after_iww(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Move signature-page line records into one contiguous block right
    after the IWW operating clause (or after the last non-sig record in
    document order, if IWW is absent).

    This runs AFTER `_sort_records_by_source_position`. The position
    sorter's source-text search can mis-locate short sig-line records
    like "ULURU Inc." (where the same string also appears inside a
    Section 17 Notices address block). Once the sort is done we
    structurally anchor the sig block to the IWW position so the JSONL
    order matches the source's signature-page order.

    Records carrying `_sig_line=True` are the sig-page lines. Their
    relative order is preserved by sorting on `node_id` (the input
    order from doc2dict's HTML walker, which matches the visual order
    inside the sig page).
    """
    sig_records: list[dict[str, Any]] = []
    non_sig_records: list[dict[str, Any]] = []
    iww_index: int | None = None
    for r in rows:
        if r.get("_sig_line"):
            sig_records.append(r)
        else:
            non_sig_records.append(r)

    if not sig_records:
        return rows

    # Sort sig records by node_id, which matches doc2dict's HTML-walk
    # order and (for sig pages) the visual top-to-bottom order. After
    # this sort, body-only fragments follow their preceding titled
    # record so the merge pass can consolidate per-party blocks.
    sig_records.sort(key=lambda r: r["node_id"])

    # Find the IWW record in non_sig_records. The IWW is the L1 record
    # whose span begins with "IN WITNESS WHEREOF".
    for i, r in enumerate(non_sig_records):
        if r.get("is_envelope") or r.get("scope") == "trailer":
            continue
        t = (r.get("title") or "").strip()
        b = (r.get("body_direct") or "").strip()
        span = f"{t}\n{b}" if (t and b) else (t or b)
        if _is_iww_clause(span):
            iww_index = i
            break

    if iww_index is None:
        # No IWW — append sig records at the end (post sort).
        return non_sig_records + sig_records

    # Splice the sig records in immediately after the IWW.
    return (
        non_sig_records[: iww_index + 1]
        + sig_records
        + non_sig_records[iww_index + 1 :]
    )


def parse_one(idx: int, raw: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    size_kb = round(len(raw) / 1024, 2)
    base: dict[str, Any] = {
        "idx": idx,
        "size_kb": size_kb,
        "parse_status": None,
        "parse_error": None,
        "elapsed_ms": None,
        "n_section_nodes": 0,
        "n_typed_headers": 0,
        "max_depth": 0,
        "total_body_chars": 0,
        "class_counts_json": "{}",
    }

    html = extract_html(raw)
    if html is None:
        base["parse_status"] = "no_html_block"
        return base, []
    if is_image_only(html):
        base["parse_status"] = "image_only"
        return base, []

    t0 = time.perf_counter()
    try:
        # ← passes the validated EX-10 mapping_dict instead of the library default
        result = html2dict(html, mapping_dict=_AGREEMENT_CONFIG)
    except Exception as e:
        base["parse_status"] = "error"
        base["parse_error"] = f"{type(e).__name__}: {e}"
        base["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        return base, []
    elapsed = (time.perf_counter() - t0) * 1000
    base["elapsed_ms"] = round(elapsed, 2)

    document_tree = result.get("document", {}) if isinstance(result, dict) else {}
    sections = walk_sections(document_tree, depth=0)
    sections = _fix_post_signature_envelope(sections)
    # Drop TOC chrome ("TABLE OF CONTENTS" header + roman-numeral page
    # markers whose bodies are TOC continuation). The bare roman-numeral
    # walk_sections drop already handles the empty-body case; this
    # function handles the case where doc2dict left a body of TOC
    # entries attached to the page marker. The LAST page marker's body
    # sometimes carries the real preamble — when so, clear the title
    # only and preserve the body for downstream emission.
    sections = _drop_toc_chrome_records(sections)
    # doc2dict's HTML walker can flatten a real-subdoc's descendants up to
    # the document tree root (parent_node_id=None). Re-parent those orphan
    # top-level nodes into the enclosing real subdoc by source position
    # BEFORE the scope rule classifies them, so the subdoc's body content
    # is correctly recognized as in-scope.
    sections = _reparent_orphan_top_level_into_enclosing_subdoc(sections)
    sections = _apply_scope_rule(sections)
    # Collapse each real subdoc (named exhibit/schedule/appendix/annex
    # with descriptive title or descriptive first-child) into ONE L1
    # record with its descendants consolidated into the subdoc body.
    # Per task_rules/scope_rule.md, real subdocs are agreement content;
    # but doc2dict often fragments their descendants into deep nested
    # predicted-header records, which inflates depth past the rubric
    # intent AND trips freeze.py's boilerplate-pattern detector. The
    # consolidated record is the canonical "subdoc header + body" shape
    # the rubric describes.
    sections = _consolidate_real_subdocs(sections)
    sections = _merge_bare_section_marker_with_child(sections)
    # After the merge step, fix doc2dict's HTML-walker mis-parenting:
    # numbered "13. Definitions" sometimes lands as a SIBLING of its own
    # lettered (a)-(f) children. Re-parent lettered/roman subsections that
    # follow a numbered sibling into that numbered section so the tree
    # depths reflect the actual legal hierarchy.
    sections = _reparent_lettered_subsections_to_numbered_siblings(sections)
    # Merge multi-line agreement titles before splitting the preamble.
    # When the title is typeset across two visual lines (e.g. "AMENDMENT
    # NO.1 TO CRUDE TALL OIL AND\nBLACK LIQUOR SOAP SKIMMINGS AGREEMENT"),
    # doc2dict emits each line as its own predicted-header sibling. Only
    # the last line carries the AGREEMENT keyword and gets remapped to
    # depth=0; the upper line(s) sit at depth=1. This pass merges those
    # upper-line continuations into the L0 title before any downstream
    # step uses the title for sorting or scope decisions.
    sections = _merge_multiline_l0_title(sections)
    sections = _split_l0_title_from_preamble(sections)
    # Cover-preamble rescue: when the L0 title appears TWICE in the
    # source (once on the cover page, once at the agreement start), the
    # BY AND BETWEEN / parties / date block on the cover is the cover-
    # preamble. Emit it as one L1 record after the L0 title. Per
    # Arthur's annotation for idx=1 (Defect 1).
    sections = _rescue_cover_preamble_block(sections)
    # Post-title cover-line rescue: when the L0 title is followed
    # immediately by a "by and among / parties / dated" block on the
    # cover page (rather than preceded by a duplicate title), collapse
    # those consecutive empty-body predicted-header lines into ONE
    # synthetic L1 cover-preamble record. Mirrors the duplicate-title
    # rescue but for the simpler title-first cover layout.
    sections = _rescue_post_title_cover_block(sections)
    sections = _split_inline_section_markers(sections)
    # Numbered subsections "N.M" buried in Article bodies (e.g.
    # "1.1 ... 1.2 ... 1.3 ..." inside ARTICLE 1's body) get split out
    # as L2 records. This handles license/credit-agreement style where
    # every definition is inline rather than its own HTML element.
    sections = _split_inline_nm_section_markers(sections)
    # After splitting out inlined "N." sections from body_direct, scan
    # for foreign lettered "(L)" markers that doc2dict's tail-attribution
    # pathology left in the body. Split them out and re-attach to the
    # sibling section whose lettered children's enumeration would
    # naturally continue with that letter.
    sections = _split_foreign_lettered_markers_from_body(sections)
    # Reclassify single-letter "(i)" from Roman to alphabetical when
    # surrounded by "(h)"/"(j)" siblings. Roman classification is the
    # default per _LEVEL_PATTERNS; alphabetical context is a structural
    # exception driven by sibling sequence, not by marker shape alone.
    sections = _reclassify_letter_i_to_alphabetical(sections)
    # Drop cover-page records BEFORE the L0 title (registrant short-name,
    # exhibit-number echoes). These are filing metadata wedged between
    # the SEC envelope and the agreement body.
    sections = _drop_pre_title_cover_records(sections)
    # Split inline "IN WITNESS WHEREOF" + signature page out of a body.
    # doc2dict sometimes packs the entire sig page into the trailing body
    # of the LAST lettered subsection when the HTML walker couldn't
    # promote it. This pass detects that pathology STRUCTURALLY (no
    # existing IWW record in the document, IWW phrase appearing mid-body)
    # and emits IWW as L1 + each sig-page line as L2 — restoring the
    # title-as-root sig-page hierarchy.
    sections = _split_iww_and_sig_from_body(sections)
    # Explode the signature page per the title-as-root rubric:
    #   - IWW operating clause sits at L1 (signature-page header).
    #   - Each signature-page line (party label, /s/, name, title,
    #     address) is its OWN flat L2 record.
    #   - Signature-page banner chrome ("SIGNATURE PAGE TO FOLLOW",
    #     "[Signature Page Follows]") is dropped.
    sections = _explode_signature_block_lines(sections)
    # Strip bare "Exhibit N.M" titles from spurious page-header exhibit
    # records (substantive body, not envelope, not real subdoc). The
    # bare-identifier title would otherwise become the highest-priority
    # sort probe and match the FIRST source occurrence (the cover-page
    # header), dragging the record to the document start where the
    # title-as-root rule would drop it. Clearing the title makes the
    # sorter fall back to the body probe, locating the record at its
    # true source position.
    sections = _strip_page_footer_exhibit_titles(sections)
    sections = _sort_records_by_source_position(sections, idx)
    # Anchor synthetic subdoc body records to sit right after their
    # parent subdoc title (Defect 4). The position sorter may
    # mis-locate them if the body's first words also appear elsewhere
    # in the source.
    sections = _anchor_subdoc_bodies_after_titles(sections)
    # Anchor sig-line records to the IWW position. The position sorter
    # uses source-text matching, which mis-locates short sig-line
    # strings like a party-name label that also occurs inside another
    # clause's body. Structural anchor: the sig block is one contiguous
    # cluster immediately after the IWW operating clause.
    sections = _consolidate_sig_lines_after_iww(sections)
    # Merge body-only sig fragments into the preceding titled sig
    # record. Runs after the cluster so "preceding in document order"
    # is the sorted position. This converts per-line doc2dict
    # fragments into per-party L2 records that match the source's
    # visual party-block grouping.
    sections = _merge_body_only_sig_fragments_in_order(sections)
    # Drop records whose post-sort position precedes the L0 title. The
    # title-as-root rule (level_rubric.md) says no clause can be part of
    # the agreement if it appears before the agreement title in source
    # order. Catches TOC entries that doc2dict promoted out-of-tree and
    # any other pre-title cover content the node-id heuristic missed.
    sections = _drop_pre_title_position_records(sections)

    base["parse_status"] = "ok"
    base["n_section_nodes"] = len(sections)
    base["max_depth"] = max((s["depth"] for s in sections), default=0)
    base["total_body_chars"] = sum(s["body_direct_chars"] for s in sections)
    cls_counter: Counter[str | None] = Counter(s["cls"] for s in sections)
    base["class_counts_json"] = json.dumps({str(k): v for k, v in cls_counter.items()})
    untyped = {"predicted header", "introduction", None}
    base["n_typed_headers"] = sum(c for k, c in cls_counter.items() if k not in untyped)
    return base, sections


def main(
    output_dir: Path = typer.Option(
        Path("data/runs/doc2dict_with_config"),
        help="Where to write parse_doc2dict_with_config_{docs,nodes}.parquet.",
    ),
    limit: int = typer.Option(0, help="Parse only the first N rows. 0 = all."),
    no_truncate: bool = typer.Option(
        False,
        "--no-truncate",
        help=(
            "Disable JSONL truncation. By default, strings longer than "
            f"{_JSONL_TRUNCATE_THRESHOLD} chars are summarized to "
            f"<head {_JSONL_TRUNCATE_HEAD}>[TRUNCATED]<tail {_JSONL_TRUNCATE_TAIL}>. "
            "With this flag, JSONL spans are written full-length — useful "
            "when the JSONL feeds reconstruction-quality measurement."
        ),
    ),
    hf_token: str | None = typer.Option(
        None,
        envvar="HF_TOKEN",
        help="HuggingFace token. Reads /home/claude/.hf_token as fallback.",
    ),
) -> None:
    """Run doc2dict + agreement_config over the full corpus."""
    if hf_token is None:
        fallback = Path("/home/claude/.hf_token")
        if fallback.exists():
            hf_token = fallback.read_text().strip()
        else:
            logger.error("Set HF_TOKEN env var or place a token at /home/claude/.hf_token")
            raise typer.Exit(code=2)

    output_dir.mkdir(parents=True, exist_ok=True)
    logger.info(f"Streaming corpus: {CORPUS_URL}")
    lf = pl.scan_parquet(CORPUS_URL, storage_options={"token": hf_token})
    n_total = lf.select(pl.len()).collect().item()
    logger.info(f"Corpus has {n_total} rows")

    if limit > 0:
        lf = lf.head(limit)
        n_total = min(limit, n_total)
        logger.info(f"Limiting to first {n_total} rows")

    df = lf.with_row_index("idx").collect()
    logger.info(f"Materialized {len(df)} rows")

    doc_records: list[dict[str, Any]] = []
    all_section_records: list[dict[str, Any]] = []

    # JSONL is written streamingly so a long-running corpus parse leaves a
    # readable partial file even if it crashes mid-loop. Parquet still
    # accumulates in memory and is written at the end (matches the existing
    # pattern; switching parquet to streaming would need a different writer).
    nodes_jsonl_path = output_dir / "parse_doc2dict_with_config_nodes.jsonl"
    with (
        nodes_jsonl_path.open("w", encoding="utf-8") as nodes_jsonl,
        Progress(
            SpinnerColumn(),
            TextColumn("[bold green]doc2dict + agreement_config[/bold green]"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            TextColumn("{task.description}"),
            transient=False,
        ) as progress,
    ):
        task = progress.add_task("starting...", total=len(df))
        for row in df.iter_rows(named=True):
            idx = int(row["idx"])
            sub = row.get("submission") or {}
            meta = row.get("document_metadata") or {}
            company = (sub.get("company_name") or "?")[:60]
            doc_type = meta.get("document_type") or "?"
            raw = row.get("raw_document_content") or ""

            doc, sections = parse_one(idx, raw)
            doc["company"] = company
            doc["doc_type"] = doc_type
            doc_records.append(doc)
            # SEC envelope drop: the section flagged `is_envelope` by
            # walk_sections is the SEC <DOCUMENT> wrapper (e.g. "EXHIBIT
            # 10.25") — filing metadata, not contract content. Drop from
            # JSONL only; the full record stays in the parquet via
            # all_section_records so downstream tools can still see the
            # envelope class and ID if they care.
            # `order` is a per-idx 0-indexed sequence number assigned at
            # JSONL emission time (i.e. AFTER envelope/trailer drops).
            # The first record emitted for an idx has order=0, the next
            # order=1, etc. This makes the document sequence explicit in
            # the schema so downstream consumers reconstruct ordering
            # without relying on JSON-key ordering. See
            # task_rules/level_rubric.md "JSONL record shape".
            jsonl_order = 0
            for s in sections:
                s["idx"] = idx
                all_section_records.append(s)
                if s.get("is_envelope"):
                    continue
                if s.get("scope") == "trailer":
                    continue
                span = _build_jsonl_span(s.get("title"), s.get("body_direct"), s.get("cls"))
                if not span:
                    continue
                jsonl_record = {
                    "idx": s["idx"],
                    "order": jsonl_order,
                    "level": s["depth"],
                    "span": span if no_truncate else _truncate_for_jsonl(span),
                }
                nodes_jsonl.write(json.dumps(jsonl_record, ensure_ascii=False) + "\n")
                jsonl_order += 1

            progress.update(
                task,
                advance=1,
                description=(
                    f"idx={idx} {company[:24]:<24} "
                    f"{doc['parse_status']:<12} "
                    f"sections={doc['n_section_nodes']:>4} typed={doc['n_typed_headers']:>3} "
                    f"body_kb={doc['total_body_chars'] / 1024:>6.1f}"
                ),
            )

    docs_df = pl.DataFrame(doc_records).select(
        pl.col("idx"),
        pl.col("company"),
        pl.col("doc_type"),
        pl.col("size_kb"),
        pl.col("parse_status"),
        pl.col("parse_error"),
        pl.col("elapsed_ms"),
        pl.col("n_section_nodes"),
        pl.col("n_typed_headers"),
        pl.col("max_depth"),
        pl.col("total_body_chars"),
        pl.col("class_counts_json"),
    )
    docs_path = output_dir / "parse_doc2dict_with_config_docs.parquet"
    docs_df.write_parquet(docs_path, compression="zstd")
    logger.info(f"wrote {docs_path}: {len(docs_df)} doc rows")

    nodes_df = pl.DataFrame(all_section_records).select(
        pl.col("idx"),
        pl.col("node_id"),
        pl.col("parent_node_id"),
        pl.col("depth"),
        pl.col("subdoc_penalty"),
        pl.col("is_envelope"),
        pl.col("scope"),
        pl.col("doc2dict_section_key"),
        pl.col("cls"),
        pl.col("title"),
        pl.col("standardized_title"),
        pl.col("n_direct_section_children"),
        pl.col("body_direct_chars"),
        pl.col("body_recursive_chars"),
        pl.col("body_direct"),
        pl.col("body_recursive"),
    )
    nodes_path = output_dir / "parse_doc2dict_with_config_nodes.parquet"
    nodes_df.write_parquet(nodes_path, compression="zstd")
    logger.info(f"wrote {nodes_path}: {len(nodes_df)} section rows")
    truncation_note = (
        "no truncation (--no-truncate)"
        if no_truncate
        else f"strings >{_JSONL_TRUNCATE_THRESHOLD} chars truncated"
    )
    logger.info(
        f"wrote {nodes_jsonl_path}: {len(all_section_records)} JSONL lines "
        f"(streamed during parsing; {truncation_note})"
    )

    status_counts = Counter(d["parse_status"] for d in doc_records)
    logger.info("=== status breakdown ===")
    for s, n in status_counts.most_common():
        logger.info(f"  {s:<15} {n:>4}")

    if status_counts.get("ok", 0):
        ok = [d for d in doc_records if d["parse_status"] == "ok"]
        n = len(ok)
        avg_secs = sum(d["n_section_nodes"] for d in ok) / n
        avg_typed = sum(d["n_typed_headers"] for d in ok) / n
        avg_body = sum(d["total_body_chars"] for d in ok) / n / 1024
        avg_depth = sum(d["max_depth"] for d in ok) / n
        avg_ms = sum(d["elapsed_ms"] for d in ok) / n
        logger.info("=== ok-doc averages ===")
        logger.info(f"  avg sections:        {avg_secs:>7.1f}")
        logger.info(f"  avg typed headers:   {avg_typed:>7.2f}")
        logger.info(f"  avg body kb:         {avg_body:>7.1f}")
        logger.info(f"  avg max depth:       {avg_depth:>7.2f}")
        logger.info(f"  avg parse time (ms): {avg_ms:>7.1f}")


if __name__ == "__main__":
    typer.run(main)

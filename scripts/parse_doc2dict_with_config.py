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
    has descriptive text beyond bare identifier. A bare identifier like
    "EXHIBIT 8.2" with NO descriptive suffix is NOT a real subdoc even if
    it has child sections — doc2dict may split a parent/child where the
    parent is just a bare label and the child is the actual content.
    Only _REAL_SUBDOC_TITLE_RE (identifier + em-dash/dash/colon + description)
    qualifies.
    """
    if is_envelope:
        return False
    if (cls or "") not in _SUBDOC_CLASSES:
        return False
    if not title:
        return False
    stripped = title.strip()
    return bool(_REAL_SUBDOC_TITLE_RE.match(stripped))


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
    annex as the SEC envelope. If an envelope node shares a parent with
    section/article siblings that appear BEFORE it, the envelope is really a
    post-agreement attachment.
    """
    envelope_rows = [r for r in rows if r.get("is_envelope")]
    if not envelope_rows:
        return rows

    by_node_id = {r["node_id"]: r for r in rows}
    children_of: dict[int | None, list[dict[str, Any]]] = {}
    for r in rows:
        children_of.setdefault(r.get("parent_node_id"), []).append(r)

    _BODY_CLASSES = frozenset({"section", "article"})
    fixed = False

    for env_row in envelope_rows:
        parent_id = env_row.get("parent_node_id")
        siblings = children_of.get(parent_id, [])
        has_prior_body = False
        for sib in siblings:
            if sib["node_id"] == env_row["node_id"]:
                break
            if (sib.get("cls") or "") in _BODY_CLASSES:
                has_prior_body = True
                break
        if not has_prior_body:
            continue

        env_row["is_envelope"] = False
        fixed = True

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


def _merge_bare_section_marker_with_child(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge a bare "N." marker record with its descriptive child.

    doc2dict sometimes splits a numbered section into:
      - parent: title="10.", empty body
      - child: title="Retroactive Effect; …", body="All agreements…"
    where the child carries the actual section description and body.
    Per the rubric, this is ONE clause at depth 1: "10. Retroactive
    Effect…". Merging keeps the JSONL clean (no orphan marker records).

    Match criteria:
      - parent.title matches `^\\d+\\.\\s*$`
      - parent.body_direct is empty
      - parent has exactly one direct child (in `rows`, i.e. node-id-
        parented; not a doc2dict text leaf)
      - child.title is non-empty and does not start with a digit marker

    On match:
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
    _HAS_MARKER_PREFIX_RE = re.compile(r"^\s*\d+\.")

    for r in rows:
        if r.get("is_envelope") or r.get("scope") == "trailer":
            continue
        title = (r.get("title") or "").strip()
        body = (r.get("body_direct") or "").strip()
        if not title or body:
            continue
        if not _BARE_MARKER_RE.match(title):
            continue
        kids = [
            c for c in children_of.get(r["node_id"], [])
            if not c.get("is_envelope") and c.get("scope") != "trailer"
        ]
        if len(kids) != 1:
            continue
        kid = kids[0]
        kid_title = (kid.get("title") or "").strip()
        if not kid_title or _HAS_MARKER_PREFIX_RE.match(kid_title):
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
        return re.sub(r"[\s\xa0]+", " ", s).strip()

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
        markers = list(re.finditer(
            r"(?:(?<=[.\s\xa0])|^)(\d+)\.\s*[A-Z]",
            body,
        ))
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

        # Alphabetical context: previous is letter "(h)" AND/OR next is
        # letter "(j)". We require at least one anchor to be a clean
        # alphabetical neighbour; both, for a strong signal.
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
        if not (prev_is_h or next_is_j):
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

      3. **Explode consolidated signature-block records to L2 lines.**
         The HTML walker often packs a party's signature block as one
         record:
            title="<party-label>"  body="By: /s/ ...\\nName: ...\\nTitle: ..."
         Per the rubric, each signature-page line is its own depth-2
         record (flat siblings; document order encodes party
         grouping). The function splits multi-line bodies into one
         L2 record per line, with the leading party-label record kept
         at depth 2 as well.

         For records that already exist as one-line fragments (the
         HTML walker fragmented the signature block across multiple
         section nodes — e.g. "/s/ <name>" with empty body, then
         "<name>" with body="Address:"), depth is simply set to
         2 + subdoc_penalty without modification.

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
            if _has_section_marker_title(parent):
                break
            p_title = (parent.get("title") or "").strip()
            p_body = (parent.get("body_direct") or "").strip()
            # Party-label ancestor: short uppercase title, body empty
            # OR body is sig-shape continuation content.
            if (
                p_title
                and _SIG_BLOCK_LABEL_RE.match(p_title)
                and (
                    not p_body
                    or _SIG_FIELD_RE.match(p_body)
                    or _SIGN_OFF_RE.search(p_body)
                )
            ):
                sig_line_node_ids.add(parent["node_id"])
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

    for carrier in sig_carriers:
        for d in _walk_descendants(carrier["node_id"]):
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
                or (d_title and not d_body)  # bare name as title
                or (not d_title and d_body and _SIG_FIELD_RE.match(d_body))
            )
            if looks_sig:
                sig_line_node_ids.add(d["node_id"])

    # PASS 3: explode each sig-line record so that depth=2 and any
    # multi-line bodies are split into per-line L2 records.
    new_rows: list[dict[str, Any]] = []
    next_id = max((r["node_id"] for r in rows), default=-1) + 1

    def _make_fragment(
        parent: dict[str, Any],
        title: str,
        body: str,
        key_suffix: str,
    ) -> dict[str, Any]:
        nonlocal next_id
        nid = next_id
        next_id += 1
        return {
            "node_id": nid,
            "parent_node_id": parent.get("parent_node_id"),
            "depth": 2 + (parent.get("subdoc_penalty") or 0),
            "doc2dict_section_key": f"_sigline_{key_suffix}_{nid}",
            "cls": "promoted text leaf",
            "title": title,
            "standardized_title": None,
            "n_direct_section_children": 0,
            "body_direct_chars": len(body),
            "body_recursive_chars": len(body),
            "body_direct": body,
            "body_recursive": body,
            "subdoc_penalty": parent.get("subdoc_penalty") or 0,
            "is_envelope": False,
            "scope": "agreement",
            "_sig_line": True,
        }

    # Each sig-line record gets a sequential `_sig_order` value that
    # records its document position within the signature page. The
    # post-sort consolidation uses this to keep parent + fragments
    # adjacent (e.g. "ULURU Inc.", then its By/Name/Title fragments,
    # then "INDEMNITEE", then its /s//name/Address fragments).
    sig_order_counter = 0

    def _next_sig_order() -> int:
        nonlocal sig_order_counter
        sig_order_counter += 1
        return sig_order_counter

    for r in rows:
        if r.get("is_envelope") or r.get("scope") == "trailer":
            new_rows.append(r)
            continue
        if r["node_id"] not in sig_line_node_ids:
            new_rows.append(r)
            continue
        # This record is a signature-page line. Set its depth to L2
        # (plus any subdoc penalty), and tag it with _sig_line so the
        # post-sort step can group all sig-line records contiguously
        # after the IWW operating clause (or at the document end if
        # IWW is absent).
        l2_depth = 2 + (r.get("subdoc_penalty") or 0)
        r["_sig_line"] = True
        r["_sig_order"] = _next_sig_order()
        title = (r.get("title") or "").strip()
        body = (r.get("body_direct") or "").strip()
        body_lines = _split_sig_block_body_into_lines(body)

        if len(body_lines) >= 2:
            # Consolidated party-block. Title stays as its own L2
            # record (party label, /s/ line, or name); body lines
            # each become their own L2 record. Fragments take the
            # next sequential sig_order so they stay grouped with
            # their parent.
            r["body_direct"] = ""
            r["body_direct_chars"] = 0
            r["depth"] = l2_depth
            new_rows.append(r)
            for line in body_lines:
                frag = _make_fragment(r, "", line, "body")
                frag["_sig_order"] = _next_sig_order()
                new_rows.append(frag)
        elif len(body_lines) == 1:
            # Single-line body — split as a separate L2 record so the
            # title and the body are distinct sig-page lines (e.g.
            # title="Vaidehi Shah", body="Address:").
            body_line = body_lines[0]
            r["body_direct"] = ""
            r["body_direct_chars"] = 0
            r["depth"] = l2_depth
            new_rows.append(r)
            frag = _make_fragment(r, "", body_line, "body")
            frag["_sig_order"] = _next_sig_order()
            new_rows.append(frag)
        else:
            # No body — just set depth.
            r["depth"] = l2_depth
            new_rows.append(r)

    return new_rows


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

    # Sort sig records by `_sig_order`, the emission-order tag set
    # during explosion. This keeps parent + fragments adjacent (e.g.
    # the ULURU Inc. record followed by its By/Name/Title fragments,
    # then INDEMNITEE followed by its /s//name/Address fragments)
    # rather than interleaving the two parties' lines.
    sig_records.sort(key=lambda r: (r.get("_sig_order", 0), r["node_id"]))

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
    sections = _apply_scope_rule(sections)
    sections = _merge_bare_section_marker_with_child(sections)
    # After the merge step, fix doc2dict's HTML-walker mis-parenting:
    # numbered "13. Definitions" sometimes lands as a SIBLING of its own
    # lettered (a)-(f) children. Re-parent lettered/roman subsections that
    # follow a numbered sibling into that numbered section so the tree
    # depths reflect the actual legal hierarchy.
    sections = _reparent_lettered_subsections_to_numbered_siblings(sections)
    sections = _split_l0_title_from_preamble(sections)
    sections = _split_inline_section_markers(sections)
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
    # Explode the signature page per the title-as-root rubric:
    #   - IWW operating clause sits at L1 (signature-page header).
    #   - Each signature-page line (party label, /s/, name, title,
    #     address) is its OWN flat L2 record.
    #   - Signature-page banner chrome ("SIGNATURE PAGE TO FOLLOW",
    #     "[Signature Page Follows]") is dropped.
    sections = _explode_signature_block_lines(sections)
    sections = _sort_records_by_source_position(sections, idx)
    # Anchor sig-line records to the IWW position. The position sorter
    # uses source-text matching, which mis-locates short sig-line
    # strings like a party-name label that also occurs inside another
    # clause's body. Structural anchor: the sig block is one contiguous
    # cluster immediately after the IWW operating clause.
    sections = _consolidate_sig_lines_after_iww(sections)

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

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
    # Level 4: roman numeral in parens — multi-char "(ii)", "(iii)" or
    # common single roman numerals "(i)", "(I)", "(v)", "(V)", "(x)", "(X)".
    (4, re.compile(r"^\(([ivx]+|[IVX]+)\)")),
    # Level 4: uppercase letter in parens "(A)", "(B)"
    (4, re.compile(r"^\(([A-Z])\)")),
    # Level 4: digit in parens "(1)", "(2)"
    (4, re.compile(r"^\((\d+)\)")),
    # Level 3: single lowercase letter in parens "(a)", "(b)", "(c)"
    (3, re.compile(r"^\(([a-z])\)")),
    # Level 2: bare number with period ("1.", "10.", "21.")
    (2, re.compile(r"^(\d+)\.")),
    # Level 2: ARTICLE heading ("ARTICLE I", "ARTICLE 3", "Article IV.")
    (2, re.compile(r"^ARTICLE\s+(\d+|[IVXLCDM]+)\.?\s*$", re.IGNORECASE)),
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


def _collect_direct_text(section: dict[str, Any]) -> str:
    out: list[str] = []
    contents = section.get("contents")
    if not isinstance(contents, dict):
        return ""
    for child in contents.values():
        if _is_text_leaf(child):
            t = child.get("text")
            if isinstance(t, str):
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
    (re.compile(r"^SIGNATURE\s+PAGE", re.IGNORECASE), 1),
    (re.compile(r"^[A-Z]+\.?\s*(Inc|Corp|LLC|Ltd|Company|Co)\.?\b", re.IGNORECASE), 1),
    (re.compile(r"^INDEMNITEE$", re.IGNORECASE), 1),
    (re.compile(r"^/s/\s"), 1),
    (re.compile(r"^SCHEDULE\s+", re.IGNORECASE), 1),
    (re.compile(r"^APPENDIX\s+", re.IGNORECASE), 1),
    (re.compile(r"^ANNEX\s+", re.IGNORECASE), 1),
    # AGREEMENT root must be LAST so EXHIBIT/SCHEDULE/APPENDIX/ANNEX patterns
    # match first for subdoc titles (e.g. "SCHEDULE A ... AGREEMENT").
    (re.compile(r"^[\w\s]+\bAGREEMENT\s*$", re.IGNORECASE), 0),
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
        return parent_level
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

    Returns None if no signature block is found.
    """
    last_sig_idx: int | None = None
    for i, r in enumerate(rows):
        span = _build_jsonl_span(r.get("title"), r.get("body_direct"), r.get("cls"))
        if _SIG_LINE_RE.search(span):
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

    if sig_parent_id is not None:
        sig_parent = by_node_id.get(sig_parent_id)
        if sig_parent is not None:
            sig_grandparent_id = sig_parent.get("parent_node_id")
            # siblings are the grandparent's children (or root-level if
            # grandparent is None — top-level sections under the document root)
            siblings = children_of.get(sig_grandparent_id, [])
            after_sig_parent = False
            for sib in siblings:
                if sib["node_id"] == sig_parent_id:
                    after_sig_parent = True
                    continue
                if after_sig_parent:
                    # This sibling and all its descendants are post-signature
                    post_sig_node_ids.add(sib["node_id"])
                    _collect_descendants(sib["node_id"])

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


def walk_sections(
    container: dict[Any, Any],
    *,
    depth: int = 0,
    parent_id: int | None = None,
    parent_level: int | None = None,
    counter: list[int] | None = None,
    subdoc_penalty: int = 0,
    envelope_seen: list[bool] | None = None,
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
        # doc is the wrapper, not a real subdocument.
        is_envelope = False
        if (cls or "") in _SUBDOC_CLASSES and not envelope_seen[0]:
            envelope_seen[0] = True
            is_envelope = True

        remapped = _remap_depth(title, cls, depth, parent_level=parent_level)
        if remapped == 0 and not body_direct.strip():
            remapped = 1
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
            1 if ((cls or "") in _SUBDOC_CLASSES and not is_envelope) else 0
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
                rows.append(
                    {
                        "node_id": prom_id,
                        "parent_node_id": node_id,
                        "depth": min(prom_remapped + child_penalty, 4 + child_penalty),
                        "doc2dict_section_key": prom_key,
                        "cls": prom_cls,
                        "title": prom_title,
                        "standardized_title": None,
                        "n_direct_section_children": 0,
                        "body_direct_chars": len(prom_body),
                        "body_recursive_chars": len(prom_full),
                        "body_direct": prom_body,
                        "body_recursive": prom_full,
                        "subdoc_penalty": child_penalty,
                        "is_envelope": False,
                    }
                )
    return rows


# Build the config ONCE per process. make_agreement_config() does a deep copy
# of STANDARD_CONFIG internally so we don't risk leaking mutation across docs.
_AGREEMENT_CONFIG = make_agreement_config()


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
    sections = _apply_scope_rule(sections)

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
                    "level": s["depth"],
                    "span": span if no_truncate else _truncate_for_jsonl(span),
                }
                nodes_jsonl.write(json.dumps(jsonl_record, ensure_ascii=False) + "\n")

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

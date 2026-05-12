#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["typer>=0.15", "rich>=13.9"]
# ///
"""Freeze the current parser output for one idx as a regression baseline.

Reads the latest parser-emitted JSONL at
    data/auto_parse/parse_doc2dict_with_config_nodes.jsonl
filters lines for the requested idx, and writes them verbatim to
    data/auto_parse/level_freeze/frozen/idx_<N>.jsonl

The frozen file is the *golden* output for that idx: any future parser run
must emit byte-identical (level, span) sequences for that idx, otherwise
`regress.py` fails. Spans are stored full-length (no truncation) so the
comparison is exact.

Updates state.json: appends idx to "frozen" if not already present, and
records a history entry with timestamp.

Usage
-----
    uv run scripts/level_loop/freeze.py 0
    uv run scripts/level_loop/freeze.py        # freezes state.current_idx
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import typer
from rich.console import Console

REPO = Path(__file__).resolve().parents[2]
DATA = REPO / "data" / "auto_parse"
STATE_PATH = DATA / "level_freeze" / "state.json"
FROZEN_DIR = DATA / "level_freeze" / "frozen"
NODES_JSONL = DATA / "parse_doc2dict_with_config_nodes.jsonl"
SOT_JSONL = DATA / "parse_source_of_truth.jsonl"
PARSER_SRC = REPO / "scripts" / "parse_doc2dict_with_config.py"
CONFIG_SRC = REPO / "src" / "clause_extract" / "agreement_config.py"

# Reconstruction-faithfulness gate (per docs/DECISIONS.md §10).
# BLOCKING — below the bar the freeze is refused. Word coverage measures
# whether concat-of-spans recovers the source's unique words; failing this
# means the parser dropped real content.
_RECON_FAIL_BAR_PCT = 95.0  # word coverage < this -> freeze refused

console = Console()


def load_state() -> dict:
    return json.loads(STATE_PATH.read_text())


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n")


def filter_idx(jsonl_path: Path, idx: int) -> list[dict]:
    """Return parser records for one idx in the order they appear."""
    out: list[dict] = []
    if not jsonl_path.exists():
        return out
    for line in jsonl_path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("idx") == idx:
            out.append(rec)
    return out


# Rubric-validation thresholds — see task_rules/level_rubric.md.
# Hard cap is 7 (level 4 inside 3 levels of subdoc nesting). We allow up
# to 7 and reject anything higher as a sign the parser is producing
# nonsense depth (typically by capturing post-signature press-release
# boilerplate or never collapsing nested HTML).
_RUBRIC_MAX_LEVEL = 7


# Content-scope filters: patterns identifying filing-trailer boilerplate
# that should NEVER appear in the JSONL. These are SEC-filing artifacts
# attached around the agreement, not contract content. If a span matches
# any of these, the parser captured it incorrectly. NOTE: the regex
# match is on the FIRST 200 chars of the span, anchored at the start.
#
# These patterns are deliberately narrow to avoid false positives on
# legitimate contract content. A press release attached as a NAMED
# subdocument ("EXHIBIT B — FORM OF JOINT PRESS RELEASE") will appear
# as a level-1 subdoc header followed by descendants — its body text
# would not start with these patterns. Raw boilerplate trailers do.
import re as _re

_BOILERPLATE_PATTERNS: list[tuple[str, _re.Pattern[str]]] = [
    (
        "press-release contact block",
        _re.compile(
            r"^[A-Z][A-Z0-9 .,&-]{0,60}(?:MEDIA|INVESTOR|PRESS|ANALYST)\s*CONTACT[:\s]",
            _re.MULTILINE,
        ),
    ),
    (
        "forward-looking-statement disclaimer",
        _re.compile(
            r"^(?:Statements\s+in\s+this\s+press\s+release|Forward[\s\-]Looking\s+Statement)",
            _re.IGNORECASE | _re.MULTILINE,
        ),
    ),
    (
        "single roman-numeral page marker (TOC)",
        _re.compile(r"^[ivxlcdm]+\s*$", _re.IGNORECASE),
    ),
    (
        "confidential treatment SEC marker",
        _re.compile(r"Confidential\s+Treatment\s+Requested", _re.IGNORECASE),
    ),
    (
        "SEC page footer",
        _re.compile(r"^EXHIBIT\s+\d+\.\d+\s*[-—]\s*PAGE\s+\d+\s*$", _re.IGNORECASE),
    ),
]


def _check_boilerplate(records: list[dict]) -> list[str]:
    """Return diagnostics for any record whose span matches filing trailer."""
    out: list[str] = []
    for r in records:
        span = (r.get("span") or "")[:200]
        if not span:
            continue
        for label, pat in _BOILERPLATE_PATTERNS:
            if pat.search(span):
                preview = (r.get("span") or "")[:80].replace("\n", " ")
                out.append(
                    f"filing-trailer boilerplate detected ({label}): "
                    f"level={r.get('level')} span={preview!r}"
                )
                break
    return out


# Monkey-patch detector: scan the parser source for forbidden code
# shapes. The bans are spelled out in task_rules/scope_rule.md. The
# detector checks three things:
#
# 1. Named blocklist constants (`*_BOILERPLATE`, `*_TRAILER_PATTERNS`,
#    `*_BAD_PHRASES`, etc.).
# 2. Any Python STRING LITERAL whose decoded content contains trailer
#    keywords ("press release", "forward looking", "media contact",
#    "about <Company>", "conference call", "confidential treatment",
#    etc.). This catches both bare quoted phrases AND phrases hidden
#    inside `re.compile(r"...")` raw-string regex patterns — the
#    workaround the agent used after the previous detector iteration.
# 3. Level-capping arithmetic.

# Trailer keyword PAIRS. A string is flagged when ALL keywords in any
# pair appear (in any order, anywhere in the string). The pair-based
# check catches the workaround the agent used last round: it embedded
# trailer phrases inside a regex alternation
# `(?:MEDIA|INVESTOR|PRESS|ANALYST)\s*CONTACT[:\s]` — a literal regex
# substring like "MEDIA CONTACT" no longer appears, but the keywords
# "MEDIA"/"INVESTOR"/etc. and "CONTACT" both do.
_TRAILER_KEYWORD_PAIRS: list[tuple[str, list[str]]] = [
    ("press release", ["press", "release"]),
    ("forward looking", ["forward", "looking"]),
    (
        "media/investor/press/analyst contact",
        ["contact", "media|investor|press|analyst"],
    ),
    ("about <company>", ["about", "momenta|csl|company|pharmaceuticals"]),
    ("conference call", ["conference", "call"]),
    ("confidential treatment", ["confidential", "treatment"]),
    ("investor relations", ["investor", "relations"]),
]


def _string_has_trailer_pair(text: str) -> str | None:
    """Return the name of the first trailer pair fully matched in text,
    or None if no pair matches.

    Each "keyword" can be a `|`-separated alternation of words; that
    alternation is treated as ONE keyword (any branch counts as a hit).
    """
    text_lower = text.lower()
    for name, keywords in _TRAILER_KEYWORD_PAIRS:
        if all(
            _re.search(rf"\b(?:{kw})\b", text_lower) for kw in keywords
        ):
            return name
    return None

# Named-blocklist constant detection. Matches identifiers like
# _JSONL_BOILERPLATE, TRAILER_PATTERNS, BAD_PHRASES, FILTER_PHRASES, etc.
_BLOCKLIST_NAME_RE = _re.compile(
    r"\b(_*(?:[A-Z][A-Z0-9]*_)*"
    r"(?:BOILERPLATE|TRAILER|BLOCKLIST|BAD_PHRASES|FILTER_PHRASES"
    r"|BANNED|FORBIDDEN_PHRASES|EXCLUDED_PHRASES|JUNK"
    r"|FILING_TRAILER|PRESS_RELEASE|TRAILER_PATTERNS"
    r"|JSONL_BOILERPLATE|PR_TRAILER|FILTER_OUT|JUNK_PATTERNS)"
    r"(?:_PATTERNS|_PHRASES|_LIST|_SET|_RE|_REGEX|_KEYWORDS)?)\b"
)

# Level-capping arithmetic.
_LEVEL_CAP_RE = _re.compile(
    r"min\s*\(\s*(?:[a-zA-Z_]*(?:level|depth)[a-zA-Z_]*)\s*,\s*\d+"
    r"|(?:level|depth)\s*=\s*min\s*\("
    r"|if\s+(?:level|depth)\s*>\s*\d+\s*:\s*\n?\s*(?:level|depth)\s*="
)


def _decode_string_literals(src: str) -> list[tuple[int, str]]:
    """Yield (line_no, decoded_string_value) for every string literal in src.

    Uses Python's tokenizer for precision — handles raw strings, triple-
    quoted strings, f-strings, byte-strings, etc.
    """
    import ast
    import io
    import tokenize as _tok
    out: list[tuple[int, str]] = []
    try:
        for tok in _tok.tokenize(io.BytesIO(src.encode("utf-8")).readline):
            if tok.type != _tok.STRING:
                continue
            # ast.literal_eval handles all Python string-literal forms.
            try:
                value = ast.literal_eval(tok.string)
            except (ValueError, SyntaxError):
                # f-strings can't be literal_eval'd — strip prefix and try
                # decoding as a plain quoted string for keyword checks.
                raw = tok.string
                # remove leading prefix letters (rbufRBUF)
                while raw and raw[0] in "rbufRBUF":
                    raw = raw[1:]
                value = raw.strip("\"'")
            if isinstance(value, str):
                out.append((tok.start[0], value))
    except _tok.TokenizeError:
        pass
    return out


def _check_monkey_patches(parser_src: str) -> list[str]:
    """Scan parser source for forbidden monkey-patch shapes.

    Returns a list of human-readable hits. Empty = clean.
    """
    out: list[str] = []
    src_lines = parser_src.splitlines()

    # 1. Named blocklist constants. Skip the parser's own legitimate
    # constants (subdoc class set, level pattern lists, etc.) by
    # restricting to the regex above which doesn't include those names.
    seen_lines: set[int] = set()
    for m in _BLOCKLIST_NAME_RE.finditer(parser_src):
        line_no = parser_src.count("\n", 0, m.start()) + 1
        if line_no in seen_lines:
            continue
        seen_lines.add(line_no)
        line_text = src_lines[line_no - 1].strip() if line_no - 1 < len(src_lines) else ""
        out.append(
            f"named blocklist constant {m.group(0)!r} at line {line_no}: "
            f"{line_text[:100]!r}"
        )

    # 2. Any string literal (raw, triple-quoted, f-string, etc.) whose
    # decoded content contains a TRAILER KEYWORD PAIR. This catches
    # phrases hidden inside re.compile(r"...press\s+release..."), inside
    # alternations (e.g. `(?:MEDIA|INVESTOR|...)\s*CONTACT`), inside
    # lists, anywhere they appear.
    seen_string_lines: set[int] = set()
    for line_no, value in _decode_string_literals(parser_src):
        if not value or len(value) < 5:
            continue
        if line_no in seen_string_lines:
            continue
        pair = _string_has_trailer_pair(value)
        if pair:
            seen_string_lines.add(line_no)
            line_text = src_lines[line_no - 1].strip() if line_no - 1 < len(src_lines) else ""
            out.append(
                f"trailer keyword pair ({pair}) in string literal at line "
                f"{line_no}: {line_text[:100]!r}"
            )

    # 3. Level-capping arithmetic.
    for m in _LEVEL_CAP_RE.finditer(parser_src):
        line_no = parser_src.count("\n", 0, m.start()) + 1
        line_text = src_lines[line_no - 1].strip() if line_no - 1 < len(src_lines) else ""
        out.append(
            f"level capping construct at line {line_no}: {line_text[:100]!r}"
        )

    return out


# ---------------------------------------------------------------------------
# Reconstruction-faithfulness measurement (non-blocking warning)
#
# The parser's only goal is to slice the source HTML such that concatenating
# the spans for one idx in JSONL line order reconstructs the source. We
# measure that with two metrics matching scripts/measure_reconstruction.py:
#
#   word_coverage_pct(idx) =
#       |source_words ∩ reconstructed_words| / |source_words| × 100
#
#   char_ratio_pct(idx) =
#       len(reconstructed_norm) / len(source_norm) × 100
#
# where the normaliser lowercases and collapses whitespace. The freeze
# itself does NOT block on low coverage — see task_rules/freeze_command.md
# for why this is informational. The bars come from docs/DECISIONS.md §10.
# ---------------------------------------------------------------------------


def _normalize_text(text: str) -> str:
    """Lowercase + whitespace-collapse for word-set / char-len comparison."""
    return " ".join((text or "").lower().split())


def _read_sot_span_clean(idx: int) -> str | None:
    """Read parse_source_of_truth.jsonl[idx].span_clean. None if missing."""
    if not SOT_JSONL.exists():
        return None
    for line in SOT_JSONL.read_text().splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("idx") == idx:
            return rec.get("span_clean") or ""
    return None


def _measure_reconstruction(idx: int, records: list[dict]) -> dict | None:
    """Compute word coverage + char ratio for one idx. None if no SoT row."""
    source = _read_sot_span_clean(idx)
    if source is None:
        return None
    reconstructed = "".join((r.get("span") or "") for r in records)
    source_norm = _normalize_text(source)
    recon_norm = _normalize_text(reconstructed)
    source_words = set(source_norm.split())
    recon_words = set(recon_norm.split())
    if not source_words:
        return None
    inter = source_words & recon_words
    word_cov = len(inter) / len(source_words) * 100
    char_ratio = (len(recon_norm) / len(source_norm) * 100) if source_norm else 0.0
    missing = source_words - recon_words
    return {
        "word_coverage_pct": word_cov,
        "char_ratio_pct": char_ratio,
        "n_source_words": len(source_words),
        "n_missing_words": len(missing),
        "sample_missing": sorted(missing)[:8],
    }


def _check_reconstruction(idx: int, records: list[dict]) -> list[str]:
    """Return a list of human-readable violations from the reconstruction gate.

    Empty list = passes (or no source-of-truth available). Non-empty = the
    freeze should be refused. Word coverage below `_RECON_FAIL_BAR_PCT` is
    the bar: any source word not appearing in the concat of spans means
    real content was dropped by the parser.
    """
    m = _measure_reconstruction(idx, records)
    if m is None:
        console.print(
            "  [dim]reconstruction: no source-of-truth row available for "
            f"idx={idx} (skipping check)[/dim]"
        )
        return []
    wc = m["word_coverage_pct"]
    cr = m["char_ratio_pct"]
    if wc >= _RECON_FAIL_BAR_PCT:
        console.print(
            f"  reconstruction OK: word_coverage={wc:.1f}% "
            f"char_ratio={cr:.1f}% (≥ {_RECON_FAIL_BAR_PCT:.0f}% bar)"
        )
        return []
    return [
        (
            f"reconstruction word_coverage={wc:.1f}% < "
            f"{_RECON_FAIL_BAR_PCT:.0f}% bar. "
            f"char_ratio={cr:.1f}%, missing {m['n_missing_words']} of "
            f"{m['n_source_words']} unique source words. "
            f"sample missing: {m['sample_missing']}"
        )
    ]


def validate_records(records: list[dict]) -> list[str]:
    """Return a list of human-readable rubric violations for `records`.

    Empty list = passes. Non-empty = the freeze should be refused.
    These checks are intentionally conservative: a real legal agreement
    that genuinely violates one of them is rare; if you have one, pass
    --force after explicit human review.
    """
    violations: list[str] = []
    if not records:
        violations.append("no records to validate")
        return violations

    n_l0 = sum(1 for r in records if r.get("level") == 0)
    if n_l0 == 0:
        violations.append(
            "rubric requires exactly one level-0 record (the agreement title "
            "alone); found zero. The parser likely failed to identify the "
            "main agreement title."
        )
    elif n_l0 > 1:
        violations.append(
            f"rubric requires exactly one level-0 record per idx; found {n_l0}. "
            "Multiple level-0 records mean the parser is splitting the agreement "
            "into multiple roots (or capturing trailing filing boilerplate as a "
            "second 'agreement')."
        )

    # `order` must be present on every record, 0-indexed, monotonic by 1.
    # See task_rules/level_rubric.md "JSONL record shape".
    orders = [r.get("order") for r in records]
    if any(o is None for o in orders):
        violations.append(
            "every record must carry an `order` field (0-indexed sequence "
            "number within the idx, document order). Found record(s) without "
            "`order` — the parser is emitting the old 3-key shape."
        )
    elif orders != list(range(len(orders))):
        violations.append(
            "`order` values must be 0, 1, 2, …, len-1 in JSONL line order. "
            f"Got order sequence starting {orders[:8]} ending {orders[-3:]} — "
            "non-monotonic or has gaps. See task_rules/level_rubric.md."
        )

    levels = [r.get("level", 0) for r in records]
    max_level = max(levels) if levels else 0
    if max_level > _RUBRIC_MAX_LEVEL:
        # Identify the offending records for the diagnostic
        deep = [
            (r.get("level"), (r.get("span") or "")[:60])
            for r in records
            if (r.get("level") or 0) > _RUBRIC_MAX_LEVEL
        ]
        violations.append(
            f"rubric ceiling is level {_RUBRIC_MAX_LEVEL} (level 4 inside 3 "
            f"levels of subdoc nesting); found max level {max_level}. "
            f"Examples of out-of-range records: {deep[:3]}"
        )

    # First emitted JSONL record should not be the SEC envelope marker
    # like "EXHIBIT 10.25" — that gets dropped by the parser. If we see
    # it here, the envelope-drop logic regressed.
    first_span = (records[0].get("span") or "").strip()
    if _re.match(r"^EXHIBIT\s+[0-9A-Za-z][0-9A-Za-z\-\.]*\s*$", first_span):
        violations.append(
            f"first JSONL record looks like the SEC envelope marker "
            f"({first_span!r}). Should be dropped by the parser. Check "
            "is_envelope handling."
        )

    # Content-scope check: any filing-trailer boilerplate is a strict
    # rejection. Do NOT cap levels to bypass — the content itself must
    # be filtered via the structural scope rule.
    bp = _check_boilerplate(records)
    if bp:
        violations.append(
            f"{len(bp)} record(s) contain SEC-filing boilerplate that is "
            "OUTSIDE the agreement scope. Filter using the STRUCTURAL "
            "scope rule (task_rules/scope_rule.md), NOT phrase matching. "
            "Examples (up to 5):"
        )
        for line in bp[:5]:
            violations.append(f"    {line}")
        if len(bp) > 5:
            violations.append(f"    … and {len(bp) - 5} more.")

    # Monkey-patch detector: scan parser source for banned code shapes
    # (phrase blocklists, level capping). See task_rules/scope_rule.md.
    if PARSER_SRC.exists():
        mp = _check_monkey_patches(PARSER_SRC.read_text())
        if mp:
            violations.append(
                f"{len(mp)} forbidden monkey-patch construct(s) detected in "
                f"{PARSER_SRC.name}. The structural scope rule must be the "
                "implementation, not phrase blocklists or level capping. "
                "Examples:"
            )
            for line in mp[:5]:
                violations.append(f"    {line}")
            if len(mp) > 5:
                violations.append(f"    … and {len(mp) - 5} more.")

    return violations


def main(
    idx: int = typer.Argument(-1, help="idx to freeze. -1 = state.current_idx"),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing freeze"),
) -> None:
    state = load_state()
    if idx < 0:
        idx = int(state["current_idx"])

    if not NODES_JSONL.exists():
        console.print(
            f"[red]Parser jsonl missing at {NODES_JSONL}.[/red] "
            "Run the parser first with "
            f"`uv run scripts/parse_doc2dict_with_config.py --limit {idx + 1} "
            f"--output-dir {DATA} --no-truncate`."
        )
        raise typer.Exit(code=2)

    # Stale-jsonl guard: if the parser wrote elsewhere (e.g. to an inner
    # data dir because of a bad cwd), the canonical jsonl will be older
    # than the freeze attempt. Refuse rather than capture stale output.
    age_s = time.time() - NODES_JSONL.stat().st_mtime
    if age_s > 300 and not force:
        console.print(
            f"[red]{NODES_JSONL} is {int(age_s)}s old.[/red] The freeze "
            "would capture stale parser output. Re-run the parser with "
            f"`--output-dir {DATA}` (ABSOLUTE path) before freezing, or "
            "pass --force if you really mean to freeze the existing file."
        )
        raise typer.Exit(code=2)

    records = filter_idx(NODES_JSONL, idx)
    if not records:
        console.print(
            f"[red]No records for idx={idx} in {NODES_JSONL}.[/red] "
            "Run the parser first with "
            f"`uv run scripts/parse_doc2dict_with_config.py --limit {idx + 1} "
            f"--output-dir {DATA} --no-truncate`."
        )
        raise typer.Exit(code=2)

    # Rubric validation gate. Refuses to freeze nonsense outputs that
    # would otherwise corrupt future regression checks.
    violations = validate_records(records)
    # Reconstruction-faithfulness gate. The parser's stated goal is
    # concat-of-spans = source; failing to recover ≥95% of unique source
    # words means content was dropped. See task_rules/freeze_command.md
    # and docs/DECISIONS.md §10.
    violations.extend(_check_reconstruction(idx, records))
    if violations and not force:
        console.print(
            f"[red]Refusing to freeze idx={idx}:[/red]"
        )
        for v in violations:
            console.print(f"  - {v}")
        console.print(
            "\nFix the parser so the output matches "
            "task_rules/level_rubric.md AND recovers ≥95% of source words, "
            "then re-run freeze. Pass --force ONLY if a human has explicitly "
            "reviewed and accepted the violations (e.g. an unusual document "
            "that legitimately can't be expressed in the rubric)."
        )
        raise typer.Exit(code=3)

    out_path = FROZEN_DIR / f"idx_{idx}.jsonl"
    if out_path.exists() and not force:
        console.print(
            f"[yellow]{out_path} already exists. Pass --force to overwrite.[/yellow]"
        )
        raise typer.Exit(code=2)

    FROZEN_DIR.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    if idx not in state["frozen"]:
        state["frozen"].append(idx)
        state["frozen"].sort()
    state["history"].append(
        {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "action": "freeze",
            "idx": idx,
            "n_records": len(records),
        }
    )
    save_state(state)

    levels = [r.get("level") for r in records]
    console.print(
        f"[green]froze idx={idx}[/green]: {len(records)} records → {out_path}"
    )
    console.print(f"  levels: {levels}")


if __name__ == "__main__":
    typer.run(main)

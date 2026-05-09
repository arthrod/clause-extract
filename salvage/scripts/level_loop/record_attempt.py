#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["typer>=0.15"]
# ///
"""Record a failed-attempt entry for one idx, so the next dispatch's
prompt can show what was tried and why it failed.

Called by run_n_turns.sh after a dispatch that did NOT advance state.
Captures:
  - validator diagnostic (from running validate_records on the current
    parser jsonl, plus monkey-patch detector on parser source);
  - parser-source diff summary vs. the snapshot saved before this
    attempt;
  - timestamp, attempt number.

Writes to data/auto_parse/level_freeze/attempts/idx_<N>.json (a list
of attempt records, appended to).

Usage
-----
    uv run scripts/level_loop/record_attempt.py <idx> <attempt_n> \\
        --snapshot <path/to/parser-before.py>
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import typer

REPO = Path(__file__).resolve().parents[2]
DATA = REPO.parent / "data" / "auto_parse"
ATTEMPTS_DIR = DATA / "level_freeze" / "attempts"
NODES_JSONL = DATA / "parse_doc2dict_with_config_nodes.jsonl"
PARSER_SRC = REPO / "scripts" / "parse_doc2dict_with_config.py"

# Reuse the validator from freeze.py so the diagnostic matches what
# freeze.py would have produced.
sys.path.insert(0, str(Path(__file__).parent))
from freeze import (  # noqa: E402
    validate_records,
    _check_monkey_patches,
    filter_idx,
)


def parser_diff_summary(snapshot_path: Path | None, current_path: Path) -> str:
    """Compute a one-line summary of how parser changed since snapshot."""
    if snapshot_path is None or not snapshot_path.exists():
        return "no parser snapshot recorded for this attempt"
    if not current_path.exists():
        return "parser source missing"
    snap = snapshot_path.read_text().splitlines()
    cur = current_path.read_text().splitlines()
    snap_n, cur_n = len(snap), len(cur)
    delta = cur_n - snap_n
    sign = "+" if delta >= 0 else ""
    snap_set = set(snap)
    cur_set = set(cur)
    added = len(cur_set - snap_set)
    removed = len(snap_set - cur_set)
    return (
        f"parser source: {snap_n} → {cur_n} lines ({sign}{delta}); "
        f"~{added} unique lines added, ~{removed} removed"
    )


def main(
    idx: int = typer.Argument(..., help="idx the attempt was for"),
    attempt_n: int = typer.Argument(..., help="1-indexed attempt number"),
    snapshot: Path | None = typer.Option(
        None,
        "--snapshot",
        help="Path to the parser source snapshot taken BEFORE the dispatch.",
    ),
    outcome: str = typer.Option(
        "no advance",
        help="Short label for the failure mode (e.g. 'no advance', "
        "'freeze rejected', 'opencode error').",
    ),
) -> None:
    ATTEMPTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = ATTEMPTS_DIR / f"idx_{idx}.json"

    # Diagnostic: run the validator against the current jsonl for this idx.
    diag_lines: list[str] = []
    if NODES_JSONL.exists():
        records = filter_idx(NODES_JSONL, idx)
        if not records:
            diag_lines.append(
                f"no records for idx={idx} in current parser jsonl — agent "
                "may not have re-run the parser, or output went to wrong dir"
            )
        else:
            violations = validate_records(records)
            if violations:
                diag_lines.append(
                    f"validator rejection — {len(violations)} violation(s):"
                )
                diag_lines.extend(violations[:20])
            else:
                diag_lines.append(
                    f"validator passed on {len(records)} records but "
                    "advance was not called by the agent"
                )

    # Monkey-patch hits (might be present even if validator passed records).
    mp_hits: list[str] = []
    if PARSER_SRC.exists():
        mp_hits = _check_monkey_patches(PARSER_SRC.read_text())

    diff_summary = parser_diff_summary(snapshot, PARSER_SRC)

    record = {
        "attempt": attempt_n,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "outcome": outcome,
        "diagnostic": "\n".join(diag_lines) if diag_lines else "(no diagnostic captured)",
        "monkey_patch_hits": mp_hits,
        "parser_diff_summary": diff_summary,
    }

    existing: list[dict] = []
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text())
        except json.JSONDecodeError:
            existing = []

    existing.append(record)
    out_path.write_text(json.dumps(existing, indent=2) + "\n")
    print(f"recorded attempt {attempt_n} for idx={idx} → {out_path}")
    print(f"  outcome: {outcome}")
    print(f"  diagnostic head: {diag_lines[0] if diag_lines else '(none)'}")
    print(f"  monkey_patch_hits: {len(mp_hits)}")


if __name__ == "__main__":
    typer.run(main)

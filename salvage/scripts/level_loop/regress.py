#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["typer>=0.15", "rich>=13.9"]
# ///
"""Regression check: every frozen idx must still produce identical output.

For each idx in state.json["frozen"], compare the current parser-emitted
records (from `data/auto_parse/parse_doc2dict_with_config_nodes.jsonl`)
against the frozen baseline (`data/auto_parse/level_freeze/frozen/idx_<N>.jsonl`).

The comparison is record-by-record, on (level, span). If any frozen idx
mismatches, regress.py prints a unified diff and exits non-zero. The loop
should not advance until this exits 0.

Why level+span and not the full dict: the JSONL only emits 3 keys
(idx, level, span) — those are the contract. node_id and other parquet
fields aren't in scope for level-tuning regressions.

Usage
-----
    uv run scripts/level_loop/regress.py
    uv run scripts/level_loop/regress.py --idx 0      # check just one
"""

from __future__ import annotations

import json
import sys
from difflib import unified_diff
from pathlib import Path

import typer
from rich.console import Console

REPO = Path(__file__).resolve().parents[2]
DATA = REPO.parent / "data" / "auto_parse"
STATE_PATH = DATA / "level_freeze" / "state.json"
FROZEN_DIR = DATA / "level_freeze" / "frozen"
NODES_JSONL = DATA / "parse_doc2dict_with_config_nodes.jsonl"

console = Console()


def load_records(path: Path, idx: int | None = None) -> list[dict]:
    out: list[dict] = []
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if idx is None or rec.get("idx") == idx:
            out.append(rec)
    return out


def to_compare_lines(records: list[dict]) -> list[str]:
    """Render each record as `level=<n> | <span>` so diffs are readable."""
    return [f"level={r.get('level')} | {r.get('span', '')}" for r in records]


def check_one(idx: int) -> bool:
    frozen_path = FROZEN_DIR / f"idx_{idx}.jsonl"
    if not frozen_path.exists():
        console.print(f"[yellow]idx={idx}: no frozen file at {frozen_path}[/yellow]")
        return False

    frozen = load_records(frozen_path)
    current = load_records(NODES_JSONL, idx=idx)

    if frozen == current:
        console.print(f"[green]idx={idx}: OK[/green] ({len(frozen)} records)")
        return True

    console.print(f"[red]idx={idx}: REGRESSION[/red]")
    console.print(f"  frozen: {len(frozen)} records, current: {len(current)} records")

    f_lines = to_compare_lines(frozen)
    c_lines = to_compare_lines(current)
    diff = list(
        unified_diff(
            f_lines,
            c_lines,
            fromfile=f"frozen/idx_{idx}.jsonl",
            tofile=f"current[idx={idx}]",
            lineterm="",
            n=1,
        )
    )
    for line in diff[:200]:
        console.print(line)
    if len(diff) > 200:
        console.print(f"... ({len(diff) - 200} more diff lines truncated)")
    return False


def main(
    idx: int = typer.Option(-1, help="Check just this idx. -1 = all frozen."),
) -> None:
    state = json.loads(STATE_PATH.read_text())
    frozen_idxs = list(state.get("frozen", []))

    if idx >= 0:
        frozen_idxs = [idx] if idx in frozen_idxs else []
        if not frozen_idxs:
            console.print(f"[yellow]idx={idx} not in frozen list.[/yellow]")
            raise typer.Exit(code=2)

    if not frozen_idxs:
        console.print("[yellow]No frozen idxs yet — nothing to regress against.[/yellow]")
        raise typer.Exit(code=0)

    console.print(f"checking {len(frozen_idxs)} frozen idx(es): {frozen_idxs}")
    all_ok = True
    for i in frozen_idxs:
        if not check_one(i):
            all_ok = False

    if not all_ok:
        console.print("[red]regression detected — fix before advancing.[/red]")
        raise typer.Exit(code=1)

    console.print("[green]all frozen idxs OK.[/green]")


if __name__ == "__main__":
    typer.run(main)

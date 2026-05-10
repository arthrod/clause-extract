#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["typer>=0.15", "rich>=13.9"]
# ///
"""Bump state.current_idx to the next sample.

Refuses to advance unless:
  1. state.current_idx is in state.frozen (i.e. the current sample has
     been frozen this turn — so we don't skip work), AND
  2. regress.py succeeds (all frozen idxs still pass).

This is the ONLY signal the loop driver looks for to "move to the next
sample." If advance succeeds, the next prompt will be generated against
the new current_idx.

Usage
-----
    uv run scripts/level_loop/advance.py
    uv run scripts/level_loop/advance.py --skip-regress   # debug only
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import typer
from rich.console import Console

REPO = Path(__file__).resolve().parents[2]
DATA = REPO / "data" / "auto_parse"
STATE_PATH = DATA / "level_freeze" / "state.json"
REGRESS = Path(__file__).parent / "regress.py"

console = Console()


def main(
    skip_regress: bool = typer.Option(
        False,
        "--skip-regress",
        help="Skip the regression gate. Use only when explicitly debugging.",
    ),
) -> None:
    state = json.loads(STATE_PATH.read_text())
    cur = int(state["current_idx"])

    if cur not in state["frozen"]:
        console.print(
            f"[red]current_idx={cur} is not frozen yet.[/red] "
            f"Freeze it first: `uv run scripts/level_loop/freeze.py {cur}`."
        )
        raise typer.Exit(code=2)

    if not skip_regress:
        result = subprocess.run(
            ["uv", "run", str(REGRESS)], capture_output=False
        )
        if result.returncode != 0:
            console.print(
                "[red]regress.py failed — refusing to advance until clean.[/red]"
            )
            raise typer.Exit(code=result.returncode)

    state["current_idx"] = cur + 1
    state["history"].append(
        {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "action": "advance",
            "from_idx": cur,
            "to_idx": cur + 1,
        }
    )
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n")
    console.print(f"[green]advanced: {cur} → {cur + 1}[/green]")


if __name__ == "__main__":
    typer.run(main)

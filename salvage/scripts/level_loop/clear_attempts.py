#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["typer>=0.15"]
# ///
"""Delete the attempts file for one idx (called after a successful advance).

The attempts file at data/auto_parse/level_freeze/attempts/idx_<N>.json
holds the failure history that's surfaced in the next dispatch's prompt.
Once an idx successfully advances, that history is no longer relevant.

Usage
-----
    uv run scripts/level_loop/clear_attempts.py <idx>
"""

from __future__ import annotations

from pathlib import Path

import typer

REPO = Path(__file__).resolve().parents[2]
DATA = REPO.parent / "data" / "auto_parse"
ATTEMPTS_DIR = DATA / "level_freeze" / "attempts"


def main(idx: int = typer.Argument(...)) -> None:
    path = ATTEMPTS_DIR / f"idx_{idx}.json"
    if path.exists():
        path.unlink()
        print(f"cleared attempts for idx={idx}: {path}")
    else:
        print(f"no attempts file for idx={idx}; nothing to clear")


if __name__ == "__main__":
    typer.run(main)

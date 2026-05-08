#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "polars>=1.18",
#     "pyarrow>=18",
#     "huggingface-hub>=0.27",
#     "loguru>=0.7",
#     "typer>=0.15",
#     "rich>=13.9",
#     "beautifulsoup4>=4.12",
#     "lxml>=5.3",
# ]
# ///
"""Source-of-truth dump for the EX-10 corpus.

Emits ONE JSONL line per source document. Each line is the *unparsed*
reference: the full HTML the parser scripts consume, plus a minimal-
processing bs4 plain-text extraction. This is the comparison baseline:

    reconstruction_quality(parser, idx) =
        similarity(
            concat(parse_<parser>_*.jsonl spans for idx),
            parse_source_of_truth.jsonl[idx]["span_clean"]
        )

Different parsers produce different reconstructions. This file is what
they're compared against.

JSONL schema (only 3 keys per line)
------------------------------------
    idx          int    Source corpus row index.
    span_clean   str    bs4 plain-text extraction of the inner HTML.
                        Minimal processing: only `<script>` and `<style>`
                        elements are decomposed (those are code, not
                        content). No paragraph-preserving newline
                        injection — `soup.get_text(separator=" ")` runs
                        with bs4's defaults so the output is the closest
                        thing to "what bs4 would give you" without
                        per-tag intervention. Word boundaries preserved
                        via the space separator.
    span_html    str    The full inner HTML between `<html...>` and the
                        last `</html>` of the SGML envelope. NOT the
                        outer `<DOCUMENT>...</DOCUMENT>` wrapper, NOT
                        just the body — the same HTML doc2dict and
                        lexnlp receive as input. Keeping it identical
                        lets you diff parser output against the source
                        without any extraction-method confound.

Both columns are UNTRUNCATED. Source of truth shouldn't be truncated;
the comparison loses meaning otherwise. Per-doc lines can reach ~300 KB
for the largest agreements; `jq` and most editors handle this.

Empty / image-only docs still get a line with empty strings — preserves
the 1:1 mapping `idx → JSONL line` for downstream joins.

Outputs
-------
One file written to --output-dir:

    parse_source_of_truth.jsonl

Usage
-----
    uv run scripts/parse_source_of_truth.py --limit 5
    HF_TOKEN=hf_xxx uv run scripts/parse_source_of_truth.py \\
        --output-dir data/runs/source_of_truth
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any

import polars as pl
import typer
from bs4 import BeautifulSoup
from loguru import logger
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

CORPUS_URL = (
    "hf://datasets/arthrod/new3_results_master22017_274.59mb"
    "/data/train-00000-of-00001.parquet"
)

# Same envelope-extraction logic as the parser scripts. Anchored to the
# same regex flavors / case-insensitivity so the HTML is byte-identical
# to what the parsers consume.
_HTML_OPEN = re.compile(r"<html[\s>]", re.IGNORECASE)
_HTML_CLOSE = re.compile(r"</html\s*>", re.IGNORECASE)


def extract_html(raw: str) -> str | None:
    """Pull the inner HTML out of the SEC <DOCUMENT> envelope.

    Returns None if no <html> block is present (e.g. envelope-only docs).
    Identical logic to the parser scripts so span_html matches exactly
    what doc2dict / lexnlp see.
    """
    m_open = _HTML_OPEN.search(raw)
    if not m_open:
        return None
    last = None
    for m in _HTML_CLOSE.finditer(raw):
        last = m
    if last is None:
        return None
    return raw[m_open.start() : last.end()]


def html_to_clean_text(html: str) -> str:
    """bs4 extraction with MINIMAL processing.

    `<script>` and `<style>` are decomposed because they're code, not
    content — every reasonable interpretation of "what does this document
    say" excludes them. Beyond that, no per-tag manipulation: no newline
    injection on block elements, no entity rewriting, no whitespace
    normalization. We use `separator=" "` (one space) instead of bs4's
    default empty separator so adjacent tags don't smash words together.
    """
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text(separator=" ")


def main(
    output_dir: Path = typer.Option(
        Path("data/runs/source_of_truth"),
        help="Where to write parse_source_of_truth.jsonl.",
    ),
    limit: int = typer.Option(
        0,
        help="Process only the first N rows. 0 = all.",
    ),
    hf_token: str | None = typer.Option(
        None,
        envvar="HF_TOKEN",
        help="HuggingFace token. Reads /home/claude/.hf_token as fallback.",
    ),
) -> None:
    """Dump per-doc source-of-truth JSONL across the corpus."""
    if hf_token is None:
        fallback = Path("/home/claude/.hf_token")
        if fallback.exists():
            hf_token = fallback.read_text().strip()
        else:
            logger.error(
                "Set HF_TOKEN env var or place a token at /home/claude/.hf_token"
            )
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

    out_path = output_dir / "parse_source_of_truth.jsonl"
    status_counts: Counter[str] = Counter()
    char_totals: dict[str, int] = {"html": 0, "clean": 0}
    elapsed_total = 0.0

    # Stream-write so a partial file remains readable on crash.
    with out_path.open("w", encoding="utf-8") as out, Progress(
        SpinnerColumn(),
        TextColumn("[bold yellow]source of truth[/bold yellow]"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        TextColumn("{task.description}"),
        transient=False,
    ) as progress:
        task = progress.add_task("starting...", total=len(df))
        for row in df.iter_rows(named=True):
            idx = int(row["idx"])
            sub = row.get("submission") or {}
            company = (sub.get("company_name") or "?")[:40]
            raw = row.get("raw_document_content") or ""

            t0 = time.perf_counter()
            html = extract_html(raw)
            if html is None:
                span_html = ""
                span_clean = ""
                status_counts["no_html_block"] += 1
            else:
                span_html = html
                try:
                    span_clean = html_to_clean_text(html)
                    status_counts["ok"] += 1
                except Exception as e:
                    span_clean = ""
                    status_counts["error"] += 1
                    logger.warning(f"  idx={idx}: bs4 failed — {type(e).__name__}: {e}")
            elapsed_total += time.perf_counter() - t0

            char_totals["html"] += len(span_html)
            char_totals["clean"] += len(span_clean)

            record = {
                "idx": idx,
                "span_clean": span_clean,
                "span_html": span_html,
            }
            out.write(json.dumps(record, ensure_ascii=False) + "\n")

            progress.update(
                task,
                advance=1,
                description=(
                    f"idx={idx} {company:<40} "
                    f"html={len(span_html) / 1024:>7.1f}KB "
                    f"clean={len(span_clean) / 1024:>7.1f}KB"
                ),
            )

    logger.info(f"wrote {out_path}: {len(df)} JSONL lines")
    logger.info("=== status breakdown ===")
    for s, n in status_counts.most_common():
        logger.info(f"  {s:<15} {n:>4}")
    logger.info("=== totals ===")
    logger.info(f"  total span_html  size: {char_totals['html'] / 1024 / 1024:>7.2f} MB")
    logger.info(f"  total span_clean size: {char_totals['clean'] / 1024 / 1024:>7.2f} MB")
    if status_counts.get("ok"):
        ok_n = status_counts["ok"]
        logger.info(f"  avg parse time per ok-doc: {elapsed_total / ok_n * 1000:>6.1f} ms")


if __name__ == "__main__":
    typer.run(main)

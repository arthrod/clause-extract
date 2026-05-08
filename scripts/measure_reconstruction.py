#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "polars>=1.18",
#     "pyarrow>=18",
#     "loguru>=0.7",
#     "typer>=0.15",
#     "rich>=13.9",
# ]
# ///
"""Measure parser reconstruction quality against the source-of-truth dump.

For every doc in the corpus, this script computes:

    word_coverage_pct(parser, idx) =
        |source_words ∩ reconstructed_words| / |source_words| × 100

    char_ratio_pct(parser, idx) =
        len(reconstructed_text_normalized) / len(source_text_normalized) × 100

where:
  - `source_words` is the set of unique whitespace-tokens in
    `parse_source_of_truth.jsonl[idx].span_clean` after lowercasing and
    whitespace collapse.
  - `reconstructed_text` is the concatenation of every span in the parser's
    JSONL for that idx, in JSONL line order (which is document order
    because the parsers walk and emit in source-text order).

100% word coverage means every source word appears at least once in the
reconstruction; it does NOT measure word-frequency accuracy. 100% char
ratio means the reconstruction has the same total character volume as
the source after whitespace normalization.

The metrics are correlated but distinct: a parser can have high word
coverage but low char ratio if it captures all the unique content but
drops repeated structural text (e.g. clause numbers). It can have high
char ratio but low word coverage if it duplicates content (e.g. emits
both an Article-level span and the Section-level child spans separately,
inflating chars while not adding new words).

Outputs
-------
One file written to --output-dir:

    reconstruction_summary.parquet
        One row per idx. Columns per parser:
            <parser>_n_spans            int    spans the parser produced
            <parser>_concat_chars       int    total raw chars in the concat
            <parser>_truncated_spans    int    spans containing [TRUNCATED]
            <parser>_word_coverage_pct  float  metric (0–100)
            <parser>_char_ratio_pct     float  metric (0–∞, often >100)
            <parser>_n_missing_words    int    |source_words \\ reconstructed_words|
        Plus:
            source_chars_norm           int    normalized source length
            source_words_unique         int    |source_words|
            min_word_coverage_pct       float  min across the parsers (sortable)
            worst_parser                str    which parser had the min

If a parser JSONL contained `[TRUNCATED]` markers, the script strips them
before measuring (so "TRUNCATED" doesn't appear as a word) but the
truncation still BIASES the result downward — the words inside the
truncated middle aren't in the reconstruction. The `<parser>_truncated_spans`
column flags how many spans were affected so you can tell biased rows
from unbiased ones. For unbiased measurement, regenerate parser JSONLs
with `--no-truncate`.

Usage
-----
    uv run scripts/measure_reconstruction.py \\
        --source-of-truth-dir data/runs/source_of_truth \\
        --d2d-baseline-dir    data/runs/doc2dict_baseline \\
        --d2d-config-dir      data/runs/doc2dict_with_config \\
        --lex-dir             data/runs/lexnlp_baseline \\
        --output-dir          data/runs/reconstruction
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import polars as pl
import typer
from loguru import logger
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

_TRUNCATED_MARKER = "[TRUNCATED]"
_WS_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Lowercase + collapse whitespace. Used before tokenization."""
    return _WS_RE.sub(" ", text).strip().lower()


def load_source_of_truth(jsonl_path: Path) -> dict[int, str]:
    """idx → span_clean. The clean text is the comparison baseline."""
    out: dict[int, str] = {}
    if not jsonl_path.exists():
        logger.error(f"Missing source-of-truth JSONL at {jsonl_path}")
        raise typer.Exit(code=2)
    with jsonl_path.open() as f:
        for line in f:
            rec = json.loads(line)
            out[int(rec["idx"])] = rec.get("span_clean") or ""
    return out


def load_parser_concat(
    jsonl_path: Path,
) -> dict[int, dict[str, Any]]:
    """idx → {concat_text, n_spans, n_truncated_spans}.

    Spans are concatenated in JSONL line order, which is document order
    for the parser scripts (they walk/emit in source-text order). The
    [TRUNCATED] marker is stripped so it doesn't count as a "word".
    """
    grouped: dict[int, dict[str, Any]] = {}
    if not jsonl_path.exists():
        logger.error(f"Missing parser JSONL at {jsonl_path}")
        raise typer.Exit(code=2)
    with jsonl_path.open() as f:
        for line in f:
            rec = json.loads(line)
            idx = int(rec["idx"])
            span = rec.get("span") or ""
            entry = grouped.setdefault(
                idx,
                {"chunks": [], "n_spans": 0, "n_truncated_spans": 0},
            )
            entry["n_spans"] += 1
            if _TRUNCATED_MARKER in span:
                entry["n_truncated_spans"] += 1
                span = span.replace(_TRUNCATED_MARKER, "")
            entry["chunks"].append(span)
    # Materialize the concat (we'll iterate once per idx, so eager is fine
    # and avoids redoing the join per measurement)
    return {
        idx: {
            "concat_text": "\n".join(d["chunks"]),
            "n_spans": d["n_spans"],
            "n_truncated_spans": d["n_truncated_spans"],
        }
        for idx, d in grouped.items()
    }


def measure(
    source_clean: str, parser_data: dict[str, Any] | None
) -> dict[str, Any]:
    """Compute the metrics for one doc-parser pair.

    Returns a dict with: n_spans, concat_chars, truncated_spans,
    word_coverage_pct, char_ratio_pct, n_missing_words. If parser_data
    is None (parser produced no spans for this idx), all metrics are 0.
    """
    sot_norm = normalize(source_clean)
    sot_words = set(sot_norm.split()) if sot_norm else set()
    sot_chars = len(sot_norm)

    if parser_data is None:
        return {
            "n_spans": 0,
            "concat_chars": 0,
            "truncated_spans": 0,
            "word_coverage_pct": 0.0,
            "char_ratio_pct": 0.0,
            "n_missing_words": len(sot_words),
        }

    concat = parser_data["concat_text"]
    concat_norm = normalize(concat)
    rec_words = set(concat_norm.split()) if concat_norm else set()
    matched = sot_words & rec_words
    missing = sot_words - rec_words

    word_coverage = (
        len(matched) / len(sot_words) * 100.0 if sot_words else 0.0
    )
    char_ratio = (
        len(concat_norm) / sot_chars * 100.0 if sot_chars else 0.0
    )
    return {
        "n_spans": parser_data["n_spans"],
        "concat_chars": len(concat),
        "truncated_spans": parser_data["n_truncated_spans"],
        "word_coverage_pct": round(word_coverage, 2),
        "char_ratio_pct": round(char_ratio, 2),
        "n_missing_words": len(missing),
    }


def main(
    source_of_truth_dir: Path = typer.Option(
        Path("data/runs/source_of_truth"),
        help="Directory containing parse_source_of_truth.jsonl",
    ),
    d2d_baseline_dir: Path = typer.Option(
        Path("data/runs/doc2dict_baseline"),
        help="Directory containing parse_doc2dict_baseline_nodes.jsonl",
    ),
    d2d_config_dir: Path = typer.Option(
        Path("data/runs/doc2dict_with_config"),
        help="Directory containing parse_doc2dict_with_config_nodes.jsonl",
    ),
    lex_dir: Path = typer.Option(
        Path("data/runs/lexnlp_baseline"),
        help="Directory containing parse_lexnlp_spans.jsonl",
    ),
    output_dir: Path = typer.Option(
        Path("data/runs/reconstruction"),
        help="Where to write reconstruction_summary.parquet",
    ),
) -> None:
    """Measure how completely each parser reconstructs the source-of-truth text."""
    output_dir.mkdir(parents=True, exist_ok=True)

    sot_path = source_of_truth_dir / "parse_source_of_truth.jsonl"
    logger.info(f"Loading source-of-truth: {sot_path}")
    source = load_source_of_truth(sot_path)
    logger.info(f"  {len(source)} source-of-truth docs")

    parsers: dict[str, Path] = {
        "d2d_baseline": d2d_baseline_dir / "parse_doc2dict_baseline_nodes.jsonl",
        "d2d_config": d2d_config_dir / "parse_doc2dict_with_config_nodes.jsonl",
        "lex": lex_dir / "parse_lexnlp_spans.jsonl",
    }
    logger.info("Loading parser JSONLs and concatenating per idx...")
    parser_data: dict[str, dict[int, dict[str, Any]]] = {}
    for name, path in parsers.items():
        data = load_parser_concat(path)
        parser_data[name] = data
        n_truncated_total = sum(d["n_truncated_spans"] for d in data.values())
        logger.info(
            f"  {name:<14} {len(data):>5} docs covered  "
            f"({n_truncated_total} truncated spans across all docs)"
        )
        if n_truncated_total > 0:
            logger.warning(
                f"  ↳ {name} JSONL contains [TRUNCATED] markers — metrics "
                f"are biased low for affected docs. Re-run the parser with "
                f"--no-truncate for unbiased measurement."
            )

    rows: list[dict[str, Any]] = []
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold cyan]measuring reconstruction[/bold cyan]"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        transient=False,
    ) as progress:
        task = progress.add_task("idx...", total=len(source))
        for idx, span_clean in source.items():
            sot_norm = normalize(span_clean)
            sot_words = set(sot_norm.split()) if sot_norm else set()
            row: dict[str, Any] = {
                "idx": idx,
                "source_chars_norm": len(sot_norm),
                "source_words_unique": len(sot_words),
            }
            per_parser_coverage: dict[str, float] = {}
            for name, data in parser_data.items():
                m = measure(span_clean, data.get(idx))
                row[f"{name}_n_spans"] = m["n_spans"]
                row[f"{name}_concat_chars"] = m["concat_chars"]
                row[f"{name}_truncated_spans"] = m["truncated_spans"]
                row[f"{name}_word_coverage_pct"] = m["word_coverage_pct"]
                row[f"{name}_char_ratio_pct"] = m["char_ratio_pct"]
                row[f"{name}_n_missing_words"] = m["n_missing_words"]
                per_parser_coverage[name] = m["word_coverage_pct"]
            if per_parser_coverage:
                worst_parser = min(
                    per_parser_coverage, key=per_parser_coverage.get  # type: ignore[arg-type]
                )
                row["min_word_coverage_pct"] = per_parser_coverage[worst_parser]
                row["worst_parser"] = worst_parser
            else:
                row["min_word_coverage_pct"] = 0.0
                row["worst_parser"] = "none"
            rows.append(row)
            progress.update(task, advance=1)

    df = pl.DataFrame(rows).sort("idx")
    out_path = output_dir / "reconstruction_summary.parquet"
    df.write_parquet(out_path, compression="zstd")
    logger.info(f"wrote {out_path}: {len(df)} rows, {len(df.columns)} cols")

    # Headline distributions per parser
    logger.info("=== reconstruction quality by parser ===")
    for name in parser_data.keys():
        col = f"{name}_word_coverage_pct"
        stats = df.select(
            pl.col(col).mean().alias("mean"),
            pl.col(col).median().alias("median"),
            pl.col(col).quantile(0.10).alias("p10"),
            pl.col(col).quantile(0.90).alias("p90"),
            pl.col(col).min().alias("min"),
            pl.col(col).max().alias("max"),
            (pl.col(col) >= 95).sum().alias("n_high"),
            (pl.col(col) < 50).sum().alias("n_low"),
        ).row(0, named=True)
        logger.info(
            f"  {name:<14} mean={stats['mean']:5.1f}  median={stats['median']:5.1f}  "
            f"p10={stats['p10']:5.1f}  p90={stats['p90']:5.1f}  "
            f"min={stats['min']:5.1f}  max={stats['max']:5.1f}  "
            f">=95: {stats['n_high']:>4}  <50: {stats['n_low']:>4}"
        )

    # Worst-parser breakdown
    worst_counts = Counter(df.get_column("worst_parser").to_list())
    logger.info("=== which parser is the weakest per doc ===")
    for parser_name, n in worst_counts.most_common():
        logger.info(f"  {parser_name:<14} weakest on {n:>5} doc(s)")

    # Top 5 most struggling docs (lowest min_word_coverage_pct)
    worst_5 = df.sort("min_word_coverage_pct").head(5)
    logger.info("=== top 5 docs with lowest min word-coverage ===")
    for r in worst_5.iter_rows(named=True):
        logger.info(
            f"  idx={r['idx']:>4}  worst={r['worst_parser']:<14} "
            f"d2d_b={r['d2d_baseline_word_coverage_pct']:5.1f}  "
            f"d2d_c={r['d2d_config_word_coverage_pct']:5.1f}  "
            f"lex={r['lex_word_coverage_pct']:5.1f}"
        )


if __name__ == "__main__":
    typer.run(main)

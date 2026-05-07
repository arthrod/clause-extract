#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "polars>=1.18",
#     "pyarrow>=18",
#     "rapidfuzz>=3.14",
#     "loguru>=0.7",
#     "typer>=0.15",
#     "rich>=13.9",
# ]
# ///
"""Compare the outputs of the three parser scripts side by side.

Reads pre-computed parquet outputs from:
  - parse_doc2dict_baseline.py    (no config — every header is `predicted header`)
  - parse_doc2dict_with_config.py (with agreement_config — typed where IDs match)
  - parse_lexnlp.py               (flat list of spans with abs_level)

Produces
--------
1. A summary parquet across all docs:
       compare_summary.parquet — one row per doc with cross-parser metrics.

2. Optional detailed per-doc markdown dumps when --dump <idxs> is supplied:
       dumps/idx_<N>.md — full-text inspection of all three parsers'
       output for that document, side by side.

Design rationale
----------------
The three parsers produce different shapes (tree vs flat list) and don't
agree on boundaries, so forcing a row-by-row alignment misleads the
reader. The dump format groups by parser instead — three independent
sections per doc, each showing what THAT parser captured. The reader
compares mentally.

The summary parquet uses rapidfuzz WRatio to measure overall body-text
agreement between doc2dict and lexnlp on each doc. Below ~80 means the
parsers captured substantially different content; above ~95 means they
mostly agree on what the doc contains (even if they disagree on
section boundaries).

Usage
-----
    # Summary across all docs (no detailed dumps):
    uv run scripts/compare.py \\
        --d2d-baseline-dir data/runs/doc2dict_baseline \\
        --d2d-config-dir   data/runs/doc2dict_with_config \\
        --lex-dir          data/runs/lexnlp_baseline \\
        --output-dir       data/runs/comparison

    # Plus detailed markdown for specific docs:
    uv run scripts/compare.py [...] --dump 0,4,68,139

    # Dump every doc that has high disagreement (body_overlap_pct < 80):
    uv run scripts/compare.py [...] --dump-disagreements
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import polars as pl
import typer
from loguru import logger
from rapidfuzz import fuzz
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_three(
    d2d_baseline_dir: Path,
    d2d_config_dir: Path,
    lex_dir: Path,
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame, pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """Load all six parquet files (3 docs + 3 nodes/spans). Validates that
    each pair exists and that idx coverage is consistent."""
    paths = {
        "d2d_baseline_docs": d2d_baseline_dir / "parse_doc2dict_baseline_docs.parquet",
        "d2d_baseline_nodes": d2d_baseline_dir / "parse_doc2dict_baseline_nodes.parquet",
        "d2d_config_docs": d2d_config_dir / "parse_doc2dict_with_config_docs.parquet",
        "d2d_config_nodes": d2d_config_dir / "parse_doc2dict_with_config_nodes.parquet",
        "lex_docs": lex_dir / "parse_lexnlp_docs.parquet",
        "lex_spans": lex_dir / "parse_lexnlp_spans.parquet",
    }
    for name, p in paths.items():
        if not p.exists():
            logger.error(f"Missing {name} at {p}")
            raise typer.Exit(code=2)
    dfs = {name: pl.read_parquet(p) for name, p in paths.items()}
    return (
        dfs["d2d_baseline_docs"],
        dfs["d2d_baseline_nodes"],
        dfs["d2d_config_docs"],
        dfs["d2d_config_nodes"],
        dfs["lex_docs"],
        dfs["lex_spans"],
    )


# ---------------------------------------------------------------------------
# Per-doc body-text aggregation
# ---------------------------------------------------------------------------


def aggregate_doc_body_d2d(nodes_df: pl.DataFrame, idx: int) -> str:
    """Concatenate all body_direct text in a doc2dict run for one doc, in
    document order (node_id ascending). Using body_direct (not recursive)
    avoids double-counting because each text leaf appears in exactly one
    section's body_direct."""
    rows = (
        nodes_df.filter(pl.col("idx") == idx)
        .sort("node_id")
        .select("body_direct")
        .to_series()
        .to_list()
    )
    return "\n".join(s for s in rows if s)


def aggregate_doc_body_lex(spans_df: pl.DataFrame, idx: int) -> str:
    """Concatenate all lex span_text in a doc, in document order (span_id).

    NOTE: lexnlp spans overlap structurally — a Section 5 span contains
    its (a)(b)(c) child text, AND there are separate spans for each (a)(b)(c).
    We use ONLY the deepest-level spans (highest abs_level among siblings
    that share a region) to avoid duplication. In practice spans are
    non-overlapping by construction (each char belongs to one span based
    on lex's section-break detection), so we just concatenate everything."""
    rows = (
        spans_df.filter(pl.col("idx") == idx)
        .sort("span_id")
        .select("span_text")
        .to_series()
        .to_list()
    )
    return "\n".join(s for s in rows if s)


def body_overlap_pct(a: str, b: str) -> float:
    """rapidfuzz WRatio between two body texts. 0–100 scale.

    rapidfuzz WRatio is O(n*m) in the worst case and doesn't scale to
    very long inputs — a 200KB×200KB comparison can take minutes. We're
    looking for a coarse agreement signal, not a perfect alignment, so
    we cap inputs at 50KB and document this limitation.

    Truncation is from both ends (start + end of doc) to capture
    structural marker overlap (envelope title, signatures) plus the
    midpoint context. This gives a more representative sample than
    just-the-start truncation.
    """
    if not a or not b:
        return 0.0
    cap = 50_000
    a_short = a if len(a) <= cap else a[: cap // 2] + a[-cap // 2 :]
    b_short = b if len(b) <= cap else b[: cap // 2] + b[-cap // 2 :]
    return float(fuzz.WRatio(a_short, b_short))


# ---------------------------------------------------------------------------
# Summary parquet
# ---------------------------------------------------------------------------


def build_summary(
    d2d_b_docs: pl.DataFrame,
    d2d_b_nodes: pl.DataFrame,
    d2d_c_docs: pl.DataFrame,
    d2d_c_nodes: pl.DataFrame,
    lex_docs: pl.DataFrame,
    lex_spans: pl.DataFrame,
) -> pl.DataFrame:
    """Per-doc summary table joining all three parsers."""
    # Join the three docs frames on idx
    base = (
        d2d_b_docs.select(
            "idx",
            "company",
            "doc_type",
            "size_kb",
            pl.col("parse_status").alias("d2d_status"),
            pl.col("n_section_nodes").alias("d2d_baseline_n_sections"),
            pl.col("n_typed_headers").alias("d2d_baseline_n_typed"),
            pl.col("max_depth").alias("d2d_baseline_max_depth"),
            pl.col("total_body_chars").alias("d2d_baseline_body_chars"),
            pl.col("elapsed_ms").alias("d2d_baseline_ms"),
        )
        .join(
            d2d_c_docs.select(
                "idx",
                pl.col("n_section_nodes").alias("d2d_config_n_sections"),
                pl.col("n_typed_headers").alias("d2d_config_n_typed"),
                pl.col("max_depth").alias("d2d_config_max_depth"),
                pl.col("total_body_chars").alias("d2d_config_body_chars"),
                pl.col("elapsed_ms").alias("d2d_config_ms"),
                pl.col("class_counts_json").alias("d2d_config_class_counts_json"),
            ),
            on="idx",
            how="full",
            coalesce=True,
        )
        .join(
            lex_docs.select(
                "idx",
                pl.col("parse_status").alias("lex_status"),
                "plain_text_kb",
                pl.col("n_spans").alias("lex_n_spans"),
                pl.col("n_titled").alias("lex_n_titled"),
                pl.col("max_abs_level").alias("lex_max_abs_level"),
                pl.col("max_rel_level").alias("lex_max_rel_level"),
                pl.col("total_span_chars").alias("lex_body_chars"),
                pl.col("elapsed_ms").alias("lex_ms"),
                pl.col("abs_level_counts_json").alias("lex_abs_level_counts_json"),
            ),
            on="idx",
            how="full",
            coalesce=True,
        )
    )

    # For docs that parsed in BOTH doc2dict and lexnlp, compute body overlap
    overlap_rows: list[dict[str, Any]] = []
    candidate_idxs = (
        base.filter(
            (pl.col("d2d_status") == "ok") & (pl.col("lex_status") == "ok")
        )
        .select("idx")
        .to_series()
        .to_list()
    )
    logger.info(
        f"Computing body-overlap metric on {len(candidate_idxs)} ok-in-both docs..."
    )
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold yellow]body overlap[/bold yellow]"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        transient=False,
    ) as progress:
        task = progress.add_task("comparing...", total=len(candidate_idxs))
        for idx in candidate_idxs:
            d2d_body = aggregate_doc_body_d2d(d2d_b_nodes, idx)
            lex_body = aggregate_doc_body_lex(lex_spans, idx)
            overlap = body_overlap_pct(d2d_body, lex_body)
            overlap_rows.append({
                "idx": idx,
                "body_overlap_pct": round(overlap, 2),
                "d2d_body_chars_actual": len(d2d_body),
                "lex_body_chars_actual": len(lex_body),
            })
            progress.update(task, advance=1)

    overlap_df = (
        pl.DataFrame(overlap_rows) if overlap_rows
        else pl.DataFrame(schema={
            "idx": pl.UInt32,
            "body_overlap_pct": pl.Float64,
            "d2d_body_chars_actual": pl.Int64,
            "lex_body_chars_actual": pl.Int64,
        })
    )
    # Cast idx to match base.idx dtype
    if not overlap_df.is_empty():
        overlap_df = overlap_df.with_columns(pl.col("idx").cast(base.schema["idx"]))

    summary = base.join(overlap_df, on="idx", how="left", coalesce=True)
    return summary.sort("idx")


# ---------------------------------------------------------------------------
# Per-doc markdown dump
# ---------------------------------------------------------------------------


def _truncate(s: str, max_chars: int) -> str:
    if not s:
        return ""
    if len(s) <= max_chars:
        return s
    return s[:max_chars].rstrip() + f"\n\n... [truncated; {len(s) - max_chars} more chars]"


def render_dump(
    idx: int,
    *,
    summary_row: dict[str, Any],
    d2d_b_nodes: pl.DataFrame,
    d2d_c_nodes: pl.DataFrame,
    lex_spans: pl.DataFrame,
    body_preview_chars: int = 500,
) -> str:
    """Build the markdown report for a single doc."""
    lines: list[str] = []

    company = summary_row.get("company") or "?"
    doc_type = summary_row.get("doc_type") or "?"
    size_kb = summary_row.get("size_kb") or 0.0

    lines.append(f"# Doc idx={idx}: {company}\n")
    lines.append(f"**doc_type**: `{doc_type}`  ")
    lines.append(f"**raw size**: {size_kb} KB  ")
    lines.append(f"**plain-text size**: {summary_row.get('plain_text_kb', 0)} KB\n")

    # Parser metrics summary
    lines.append("## Parser metrics\n")
    lines.append("| metric | doc2dict baseline | doc2dict + agreement_config | lexnlp |")
    lines.append("|---|---:|---:|---:|")

    def m(parser: str, key: str) -> str:
        v = summary_row.get(f"{parser}_{key}")
        if v is None:
            return "-"
        if isinstance(v, float):
            return f"{v:.1f}"
        return str(v)

    lines.append(
        f"| status | `{summary_row.get('d2d_status', '-')}` | "
        f"`{summary_row.get('d2d_status', '-')}` | "
        f"`{summary_row.get('lex_status', '-')}` |"
    )
    lines.append(
        f"| sections / spans | {m('d2d_baseline','n_sections')} | "
        f"{m('d2d_config','n_sections')} | "
        f"{summary_row.get('lex_n_spans', '-')} |"
    )
    lines.append(
        f"| typed | {m('d2d_baseline','n_typed')} (always 0) | "
        f"{m('d2d_config','n_typed')} | "
        f"{summary_row.get('lex_n_titled', '-')} (titled) |"
    )
    lines.append(
        f"| max depth / level | {m('d2d_baseline','max_depth')} | "
        f"{m('d2d_config','max_depth')} | "
        f"abs={summary_row.get('lex_max_abs_level', '-')} rel={summary_row.get('lex_max_rel_level', '-')} |"
    )
    lines.append(
        f"| body chars | {m('d2d_baseline','body_chars')} | "
        f"{m('d2d_config','body_chars')} | "
        f"{summary_row.get('lex_body_chars', '-')} |"
    )
    lines.append(
        f"| parse time (ms) | {m('d2d_baseline','ms')} | "
        f"{m('d2d_config','ms')} | "
        f"{summary_row.get('lex_ms', '-')} |"
    )
    overlap = summary_row.get("body_overlap_pct")
    if overlap is not None:
        lines.append(
            f"\n**Body-text overlap** (doc2dict baseline ↔ lexnlp, rapidfuzz WRatio): "
            f"**{overlap:.1f}** / 100\n"
        )

    # Class breakdown in config variant
    class_counts_json = summary_row.get("d2d_config_class_counts_json")
    if class_counts_json:
        cc = json.loads(class_counts_json)
        if cc:
            lines.append("**doc2dict + agreement_config class counts:**")
            for k, v in sorted(cc.items(), key=lambda x: -x[1]):
                lines.append(f"- `{k}`: {v}")
            lines.append("")

    abs_counts_json = summary_row.get("lex_abs_level_counts_json")
    if abs_counts_json:
        ac = json.loads(abs_counts_json)
        if ac:
            lines.append("**lexnlp abs_level counts:**")
            for k, v in sorted(ac.items(), key=lambda x: int(x[0])):
                lines.append(f"- abs_level `{k}`: {v}")
            lines.append("")

    # Doc2dict baseline section dump
    lines.append("---\n")
    lines.append("## doc2dict baseline (no config)\n")
    lines.append(
        "Every section's `cls` is `predicted header` because no `mapping_dict` "
        "was passed; the library has no `levels` regex to type headers with.\n"
    )
    db = d2d_b_nodes.filter(pl.col("idx") == idx).sort("node_id")
    if db.is_empty():
        lines.append("_No sections (likely image-only or parse error)._\n")
    else:
        for row in db.iter_rows(named=True):
            indent = "  " * row["depth"]
            title = row["title"] or ""
            cls = row["cls"] or ""
            std = f" → `{row['standardized_title']}`" if row["standardized_title"] else ""
            lines.append(
                f"\n### {indent}node_id={row['node_id']} depth={row['depth']} `{cls}`{std}\n"
            )
            lines.append(f"**title**: {title!r}\n")
            lines.append(
                f"**body_direct** ({row['body_direct_chars']} chars), "
                f"**body_recursive** ({row['body_recursive_chars']} chars)\n"
            )
            if row["body_direct"]:
                lines.append("```")
                lines.append(_truncate(row["body_direct"], body_preview_chars))
                lines.append("```")

    # Doc2dict + agreement_config dump
    lines.append("\n---\n")
    lines.append("## doc2dict + agreement_config (typed where structural IDs match)\n")
    dc = d2d_c_nodes.filter(pl.col("idx") == idx).sort("node_id")
    if dc.is_empty():
        lines.append("_No sections._\n")
    else:
        # Show only typed sections in detail; summarize untyped count
        typed = dc.filter(
            ~pl.col("cls").is_in(["predicted header", "introduction", None])
        )
        untyped_count = len(dc) - len(typed)
        lines.append(
            f"_{len(typed)} typed sections shown below; {untyped_count} untyped "
            f"`predicted header`/`introduction` rows omitted (their content matches "
            f"the baseline above)._\n"
        )
        for row in typed.iter_rows(named=True):
            indent = "  " * row["depth"]
            cls = row["cls"]
            std = row["standardized_title"] or ""
            title = row["title"] or ""
            lines.append(
                f"\n### {indent}node_id={row['node_id']} depth={row['depth']} `{cls}` → `{std}`\n"
            )
            lines.append(f"**title**: {title!r}\n")
            lines.append(
                f"**body_direct** ({row['body_direct_chars']} chars), "
                f"**body_recursive** ({row['body_recursive_chars']} chars)\n"
            )
            if row["body_direct"]:
                lines.append("```")
                lines.append(_truncate(row["body_direct"], body_preview_chars))
                lines.append("```")

    # Lexnlp dump
    lines.append("\n---\n")
    lines.append("## lexnlp (regex section spans)\n")
    lines.append(
        "Flat list of spans, NOT a tree. Spans don't nest — each char of the "
        "doc's plain text belongs to exactly one span. So a `Section 5` span "
        "and its `(a)`/`(b)`/`(c)` children are siblings here, not parent/child.\n"
    )
    ls = lex_spans.filter(pl.col("idx") == idx).sort("span_id")
    if ls.is_empty():
        lines.append("_No spans._\n")
    else:
        for row in ls.iter_rows(named=True):
            title = row["title"] or "(untitled)"
            indent = "  " * (row["level"] or 0)
            lines.append(
                f"\n### {indent}span_id={row['span_id']} "
                f"abs_level={row['abs_level']} level={row['level']}\n"
            )
            lines.append(f"**title**: {title!r}\n")
            lines.append(f"**span_chars**: {row['span_chars']}\n")
            if row["span_text"]:
                lines.append("```")
                lines.append(_truncate(row["span_text"], body_preview_chars))
                lines.append("```")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(
    d2d_baseline_dir: Path = typer.Option(
        Path("data/runs/doc2dict_baseline"),
        help="Directory containing parse_doc2dict_baseline_{docs,nodes}.parquet",
    ),
    d2d_config_dir: Path = typer.Option(
        Path("data/runs/doc2dict_with_config"),
        help="Directory containing parse_doc2dict_with_config_{docs,nodes}.parquet",
    ),
    lex_dir: Path = typer.Option(
        Path("data/runs/lexnlp_baseline"),
        help="Directory containing parse_lexnlp_{docs,spans}.parquet",
    ),
    output_dir: Path = typer.Option(
        Path("data/runs/comparison"),
        help="Where to write compare_summary.parquet and dumps/",
    ),
    dump: str = typer.Option(
        "",
        help="Comma-separated list of corpus idx values to dump as detailed markdown reports.",
    ),
    dump_disagreements: bool = typer.Option(
        False,
        "--dump-disagreements",
        help="Dump every doc with body_overlap_pct < 80.",
    ),
    body_preview_chars: int = typer.Option(
        500,
        help="Per-section body preview length in markdown dumps. 0 = full text (large files).",
    ),
) -> None:
    """Compare three parser outputs side by side."""
    output_dir.mkdir(parents=True, exist_ok=True)
    dumps_dir = output_dir / "dumps"

    logger.info("Loading parquets from all three runs...")
    db_docs, db_nodes, dc_docs, dc_nodes, lex_docs_df, lex_spans = load_three(
        d2d_baseline_dir, d2d_config_dir, lex_dir
    )
    logger.info(
        f"  d2d baseline: {len(db_docs)} docs, {len(db_nodes)} sections"
    )
    logger.info(
        f"  d2d config:   {len(dc_docs)} docs, {len(dc_nodes)} sections"
    )
    logger.info(
        f"  lexnlp:       {len(lex_docs_df)} docs, {len(lex_spans)} spans"
    )

    summary = build_summary(
        db_docs, db_nodes, dc_docs, dc_nodes, lex_docs_df, lex_spans
    )
    summary_path = output_dir / "compare_summary.parquet"
    summary.write_parquet(summary_path, compression="zstd")
    logger.info(f"wrote {summary_path}: {len(summary)} rows")

    # Headline summary printed to console
    if "body_overlap_pct" in summary.columns and not summary.is_empty():
        ok = summary.filter(pl.col("body_overlap_pct").is_not_null())
        if not ok.is_empty():
            stats = ok.select(
                pl.col("body_overlap_pct").mean().alias("mean"),
                pl.col("body_overlap_pct").median().alias("median"),
                pl.col("body_overlap_pct").min().alias("min"),
                pl.col("body_overlap_pct").max().alias("max"),
                (pl.col("body_overlap_pct") >= 95).sum().alias("n_strong_agree"),
                (pl.col("body_overlap_pct") < 80).sum().alias("n_disagree"),
            ).row(0, named=True)
            logger.info("=== body overlap (rapidfuzz WRatio) across ok-in-both docs ===")
            logger.info(f"  n docs:                    {len(ok)}")
            logger.info(f"  mean / median:             {stats['mean']:.1f} / {stats['median']:.1f}")
            logger.info(f"  min / max:                 {stats['min']:.1f} / {stats['max']:.1f}")
            logger.info(f"  strong agreement (>=95):   {stats['n_strong_agree']}")
            logger.info(f"  disagreement (<80):        {stats['n_disagree']}")

    # Status combinations
    status_pairs = (
        summary.group_by(["d2d_status", "lex_status"])
        .len()
        .sort("len", descending=True)
    )
    logger.info("=== status combinations (d2d, lex) ===")
    for row in status_pairs.iter_rows(named=True):
        logger.info(
            f"  d2d={row['d2d_status']!r:<14} lex={row['lex_status']!r:<14} "
            f"count={row['len']}"
        )

    # Determine dump set
    dump_idxs: list[int] = []
    if dump:
        try:
            dump_idxs.extend(int(x.strip()) for x in dump.split(",") if x.strip())
        except ValueError:
            logger.error(f"--dump expects comma-separated integers, got: {dump!r}")
            raise typer.Exit(code=2)
    if dump_disagreements and "body_overlap_pct" in summary.columns:
        disagreement_idxs = (
            summary.filter(pl.col("body_overlap_pct") < 80)
            .select("idx")
            .to_series()
            .to_list()
        )
        dump_idxs.extend(int(x) for x in disagreement_idxs)
    dump_idxs = sorted(set(dump_idxs))

    if dump_idxs:
        dumps_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"writing detailed dumps for {len(dump_idxs)} doc(s) → {dumps_dir}")
        # Index summary by idx for fast lookup
        summary_by_idx = {row["idx"]: row for row in summary.iter_rows(named=True)}
        with Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]dumps[/bold blue]"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            transient=False,
        ) as progress:
            task = progress.add_task("rendering...", total=len(dump_idxs))
            for idx in dump_idxs:
                if idx not in summary_by_idx:
                    logger.warning(f"  idx={idx} not found in summary; skipping")
                    progress.update(task, advance=1)
                    continue
                md = render_dump(
                    idx,
                    summary_row=summary_by_idx[idx],
                    d2d_b_nodes=db_nodes,
                    d2d_c_nodes=dc_nodes,
                    lex_spans=lex_spans,
                    body_preview_chars=body_preview_chars,
                )
                (dumps_dir / f"idx_{idx}.md").write_text(md, encoding="utf-8")
                progress.update(task, advance=1)
        logger.info(f"wrote {len(dump_idxs)} markdown dumps to {dumps_dir}")
    else:
        logger.info(
            "no --dump idxs supplied; skipping per-doc dumps. "
            "Pass --dump 0,4,68 or --dump-disagreements to inspect specific docs."
        )


if __name__ == "__main__":
    typer.run(main)

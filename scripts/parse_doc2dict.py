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
"""Baseline parser using doc2dict's STANDARD_CONFIG.

NO customization. NO mapping_dict tweaks. NO levels regex. We call
`html2dict(content)` with whatever the library does by default and write
out the resulting tree.

Inputs
------
The 1066-row corpus at `arthrod/new3_results_master22017_274.59mb` (HF
private). Each row has `raw_document_content` — a SEC SGML envelope
wrapping HTML. We extract the HTML between `<html...>` and the last
`</html>` and feed that to doc2dict.

Outputs
-------
Two parquet files written to --output-dir:

    parse_doc2dict_docs.parquet     One row per source document.
        idx                  : int      Source corpus row index.
        company              : string   Company name from submission metadata.
        doc_type             : string   e.g. 'EX-10.4'.
        size_kb              : float    Raw HTML size in KB.
        parse_status         : string   ok | image_only | no_html_block | error.
        parse_error          : string   Exception text if status='error', else null.
        elapsed_ms           : float    Parse time in ms.
        n_nodes              : int      Total nodes in the parsed dict.
        n_typed_headers      : int      Nodes with a non-default class
                                        (i.e., not 'predicted header' / 'introduction' / null).
        max_depth            : int      Deepest tree depth observed.
        class_counts_json    : string   JSON map of class → count.

    parse_doc2dict_nodes.parquet    One row per node in every doc's tree.
        idx                  : int      Source corpus row index.
        node_id              : int      Stable in-doc index (creation order).
        depth                : int      Tree depth (0 = top of document body).
        cls                  : string   doc2dict's `class` for the node.
        title                : string   The header text doc2dict assigned.
        standardized_title   : string   doc2dict's normalized form (or null).
        n_contents           : int      Direct-child count.
        body_chars           : int      Sum of text/textsmall character counts
                                        within this node and its descendants.

Why two parquets
----------------
Per-document is the join key for downstream work. Per-node lets you do
node-level aggregations (depth distribution, class frequency) without
re-parsing.

Usage
-----
    HF_TOKEN=hf_xxx uv run scripts/parse_doc2dict.py \\
        --output-dir data/runs/doc2dict_baseline \\
        --limit 1066

    # Smoke test:
    HF_TOKEN=hf_xxx uv run scripts/parse_doc2dict.py --limit 5
"""
from __future__ import annotations

import json
import os
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

CORPUS_URL = (
    "hf://datasets/arthrod/new3_results_master22017_274.59mb"
    "/data/train-00000-of-00001.parquet"
)

# SEC SGML envelope: <DOCUMENT>...<TEXT><html>...</html></TEXT></DOCUMENT>
_HTML_OPEN = re.compile(r"<html[\s>]", re.IGNORECASE)
_HTML_CLOSE = re.compile(r"</html\s*>", re.IGNORECASE)
# Image-only detector: less than 200 chars of visible text, mostly img tags
_IMG_TAG = re.compile(r"<img\b", re.IGNORECASE)
_TAG_STRIP = re.compile(r"<[^>]+>")


def extract_html(raw: str) -> str | None:
    """Pull the inner HTML out of the SEC <DOCUMENT> envelope.

    Returns None if no <html> block is found (some docs are envelope-only
    with images or plain text).
    """
    m_open = _HTML_OPEN.search(raw)
    if not m_open:
        return None
    # Find the LAST </html> in case of nested or repeated tags
    last_close = None
    for m in _HTML_CLOSE.finditer(raw):
        last_close = m
    if last_close is None:
        return None
    return raw[m_open.start() : last_close.end()]


def is_image_only(html: str) -> bool:
    """Detect docs that are just <img> references with no real text."""
    visible = _TAG_STRIP.sub(" ", html)
    visible = re.sub(r"\s+", " ", visible).strip()
    n_imgs = len(_IMG_TAG.findall(html))
    return len(visible) < 200 and n_imgs >= 1


def walk_tree(
    node: dict[str, Any],
    *,
    depth: int = 0,
    counter: list[int] | None = None,
) -> list[dict[str, Any]]:
    """Walk a doc2dict tree, emit one record per node.

    Parameters
    ----------
    node : the {section_id: {...}} dict OR an inner section's dict.
    depth : 0 for the top-level document body; increments per nesting level.
    counter : single-element list used as a mutable counter for stable node_ids.

    Returns
    -------
    list of dicts, one per encountered "section" in the tree.
    """
    if counter is None:
        counter = [0]
    out: list[dict[str, Any]] = []
    if not isinstance(node, dict):
        return out

    # If this dict looks like a {id: section, id: section, ...} container,
    # walk its values. Otherwise treat as a single section node.
    children_iter: list[dict[str, Any]] = []
    if all(
        isinstance(v, dict) and ("title" in v or "contents" in v or "class" in v)
        for v in node.values()
        if not isinstance(v, str)
    ) and node:
        children_iter = list(node.values())
    else:
        children_iter = [node]

    for child in children_iter:
        if not isinstance(child, dict):
            continue
        title = child.get("title")
        cls = child.get("class")
        std = child.get("standardized_title")
        contents = child.get("contents", {}) or {}
        body_chars = _sum_body_chars(child)
        n_id = counter[0]
        counter[0] += 1
        out.append({
            "node_id": n_id,
            "depth": depth,
            "cls": cls,
            "title": title,
            "standardized_title": std,
            "n_contents": len(contents) if isinstance(contents, dict) else 0,
            "body_chars": body_chars,
        })
        if isinstance(contents, dict) and contents:
            out.extend(walk_tree(contents, depth=depth + 1, counter=counter))
    return out


def _sum_body_chars(section: dict[str, Any]) -> int:
    """Sum text/textsmall character counts within a section and its children.

    doc2dict represents body text inside section dicts via 'text' and
    'textsmall' keys (strings) and nested 'contents' dicts.
    """
    total = 0
    if not isinstance(section, dict):
        return 0
    for k, v in section.items():
        if k in ("text", "textsmall") and isinstance(v, str):
            total += len(v)
        elif k == "contents" and isinstance(v, dict):
            for child in v.values():
                if isinstance(child, dict):
                    total += _sum_body_chars(child)
    return total


def parse_one(idx: int, raw: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Parse a single corpus row. Returns (doc_summary_record, node_records)."""
    size_kb = round(len(raw) / 1024, 2)
    base = {
        "idx": idx,
        "size_kb": size_kb,
        "parse_status": None,
        "parse_error": None,
        "elapsed_ms": None,
        "n_nodes": 0,
        "n_typed_headers": 0,
        "max_depth": 0,
        "class_counts_json": "{}",
    }
    nodes: list[dict[str, Any]] = []

    html = extract_html(raw)
    if html is None:
        base["parse_status"] = "no_html_block"
        return base, nodes
    if is_image_only(html):
        base["parse_status"] = "image_only"
        return base, nodes

    t0 = time.perf_counter()
    try:
        # NO customization — bare html2dict call. The library uses its
        # STANDARD_CONFIG by default.
        result = html2dict(html)
    except Exception as e:
        base["parse_status"] = "error"
        base["parse_error"] = f"{type(e).__name__}: {e}"
        base["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        return base, nodes
    elapsed = (time.perf_counter() - t0) * 1000
    base["elapsed_ms"] = round(elapsed, 2)

    document_tree = result.get("document", {}) if isinstance(result, dict) else {}
    nodes = walk_tree(document_tree, depth=0)
    base["parse_status"] = "ok"
    base["n_nodes"] = len(nodes)
    base["max_depth"] = max((n["depth"] for n in nodes), default=0)
    cls_counter = Counter(n["cls"] for n in nodes)
    base["class_counts_json"] = json.dumps({str(k): v for k, v in cls_counter.items()})
    # "Typed" = anything not the visual-default fallback.
    untyped = {"predicted header", "introduction", None}
    base["n_typed_headers"] = sum(c for k, c in cls_counter.items() if k not in untyped)
    return base, nodes


def main(
    output_dir: Path = typer.Option(
        Path("data/runs/doc2dict_baseline"),
        help="Where to write parse_doc2dict_docs.parquet and parse_doc2dict_nodes.parquet.",
    ),
    limit: int = typer.Option(0, help="Parse only the first N rows. 0 = all."),
    hf_token: str | None = typer.Option(
        None,
        envvar="HF_TOKEN",
        help="HuggingFace token. Reads /home/claude/.hf_token as fallback.",
    ),
) -> None:
    """Run baseline doc2dict over the full corpus."""
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
    all_node_records: list[dict[str, Any]] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold cyan]doc2dict baseline[/bold cyan]"),
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
            meta = row.get("document_metadata") or {}
            company = (sub.get("company_name") or "?")[:60]
            doc_type = meta.get("document_type") or "?"
            raw = row.get("raw_document_content") or ""

            doc, nodes = parse_one(idx, raw)
            doc["company"] = company
            doc["doc_type"] = doc_type
            doc_records.append(doc)
            for n in nodes:
                n["idx"] = idx
                all_node_records.append(n)

            progress.update(
                task,
                advance=1,
                description=(
                    f"idx={idx} {company[:24]:<24} "
                    f"status={doc['parse_status']:<12} "
                    f"nodes={doc['n_nodes']:>4} typed={doc['n_typed_headers']:>3}"
                ),
            )

    # Write outputs
    docs_df = pl.DataFrame(doc_records).select(
        pl.col("idx"),
        pl.col("company"),
        pl.col("doc_type"),
        pl.col("size_kb"),
        pl.col("parse_status"),
        pl.col("parse_error"),
        pl.col("elapsed_ms"),
        pl.col("n_nodes"),
        pl.col("n_typed_headers"),
        pl.col("max_depth"),
        pl.col("class_counts_json"),
    )
    docs_path = output_dir / "parse_doc2dict_docs.parquet"
    docs_df.write_parquet(docs_path, compression="zstd")
    logger.info(f"wrote {docs_path}: {len(docs_df)} doc rows")

    nodes_df = pl.DataFrame(all_node_records).select(
        pl.col("idx"),
        pl.col("node_id"),
        pl.col("depth"),
        pl.col("cls"),
        pl.col("title"),
        pl.col("standardized_title"),
        pl.col("n_contents"),
        pl.col("body_chars"),
    )
    nodes_path = output_dir / "parse_doc2dict_nodes.parquet"
    nodes_df.write_parquet(nodes_path, compression="zstd")
    logger.info(f"wrote {nodes_path}: {len(nodes_df)} node rows")

    # Headline summary
    status_counts = Counter(d["parse_status"] for d in doc_records)
    logger.info("=== status breakdown ===")
    for s, n in status_counts.most_common():
        logger.info(f"  {s:<15} {n:>4}")

    if status_counts.get("ok", 0):
        ok = [d for d in doc_records if d["parse_status"] == "ok"]
        n_total_nodes = sum(d["n_nodes"] for d in ok)
        n_total_typed = sum(d["n_typed_headers"] for d in ok)
        avg_nodes = n_total_nodes / len(ok)
        avg_typed = n_total_typed / len(ok)
        avg_depth = sum(d["max_depth"] for d in ok) / len(ok)
        avg_ms = sum(d["elapsed_ms"] for d in ok) / len(ok)
        logger.info("=== ok-doc averages ===")
        logger.info(f"  avg nodes:           {avg_nodes:>7.1f}")
        logger.info(f"  avg typed headers:   {avg_typed:>7.2f}")
        logger.info(f"  avg max depth:       {avg_depth:>7.2f}")
        logger.info(f"  avg parse time (ms): {avg_ms:>7.1f}")


if __name__ == "__main__":
    typer.run(main)

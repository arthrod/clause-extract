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
    parse_doc2dict_with_config_nodes.jsonl      Line-delimited mirror of
                                                the nodes parquet — same
                                                content, one section per
                                                line, `idx` first. Streams
                                                during parsing so a partial
                                                file remains readable on
                                                crash.

The JSONL has the same fields as the nodes parquet (idx, node_id,
parent_node_id, depth, doc2dict_section_key, cls, title,
standardized_title, n_direct_section_children, body_direct_chars,
body_recursive_chars, body_direct, body_recursive). It is NOT
zstd-compressed and contains the full body text uncapped — useful for
direct inspection via `cat | jq`, but larger on disk than the parquet.

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
    "hf://datasets/arthrod/new3_results_master22017_274.59mb"
    "/data/train-00000-of-00001.parquet"
)

_HTML_OPEN = re.compile(r"<html[\s>]", re.IGNORECASE)
_HTML_CLOSE = re.compile(r"</html\s*>", re.IGNORECASE)
_IMG_TAG = re.compile(r"<img\b", re.IGNORECASE)
_TAG_STRIP = re.compile(r"<[^>]+>")


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


def walk_sections(
    container: dict[Any, Any],
    *,
    depth: int = 0,
    parent_id: int | None = None,
    counter: list[int] | None = None,
) -> list[dict[str, Any]]:
    if counter is None:
        counter = [0]
    rows: list[dict[str, Any]] = []
    if not isinstance(container, dict):
        return rows

    for section_key, section in container.items():
        if not _is_section_node(section):
            continue
        node_id = counter[0]
        counter[0] += 1
        title = section.get("title")
        cls = section.get("class")
        std = section.get("standardized_title")
        body_direct = _collect_direct_text(section)
        body_recursive = _collect_recursive_text(section)
        contents = section.get("contents") or {}
        n_direct_children = (
            sum(1 for c in contents.values() if _is_section_node(c))
            if isinstance(contents, dict)
            else 0
        )
        rows.append({
            "node_id": node_id,
            "parent_node_id": parent_id,
            "depth": depth,
            "doc2dict_section_key": str(section_key),
            "cls": cls,
            "title": title,
            "standardized_title": std,
            "n_direct_section_children": n_direct_children,
            "body_direct_chars": len(body_direct),
            "body_recursive_chars": len(body_recursive),
            "body_direct": body_direct,
            "body_recursive": body_recursive,
        })
        if isinstance(contents, dict):
            child_sections = {
                k: v for k, v in contents.items() if _is_section_node(v)
            }
            if child_sections:
                rows.extend(
                    walk_sections(
                        child_sections,
                        depth=depth + 1,
                        parent_id=node_id,
                        counter=counter,
                    )
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

    base["parse_status"] = "ok"
    base["n_section_nodes"] = len(sections)
    base["max_depth"] = max((s["depth"] for s in sections), default=0)
    base["total_body_chars"] = sum(s["body_direct_chars"] for s in sections)
    cls_counter: Counter[str | None] = Counter(s["cls"] for s in sections)
    base["class_counts_json"] = json.dumps(
        {str(k): v for k, v in cls_counter.items()}
    )
    untyped = {"predicted header", "introduction", None}
    base["n_typed_headers"] = sum(c for k, c in cls_counter.items() if k not in untyped)
    return base, sections


def main(
    output_dir: Path = typer.Option(
        Path("data/runs/doc2dict_with_config"),
        help="Where to write parse_doc2dict_with_config_{docs,nodes}.parquet.",
    ),
    limit: int = typer.Option(0, help="Parse only the first N rows. 0 = all."),
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

    doc_records: list[dict[str, Any]] = []
    all_section_records: list[dict[str, Any]] = []

    # JSONL is written streamingly so a long-running corpus parse leaves a
    # readable partial file even if it crashes mid-loop. Parquet still
    # accumulates in memory and is written at the end (matches the existing
    # pattern; switching parquet to streaming would need a different writer).
    nodes_jsonl_path = output_dir / "parse_doc2dict_with_config_nodes.jsonl"
    with nodes_jsonl_path.open("w", encoding="utf-8") as nodes_jsonl, Progress(
        SpinnerColumn(),
        TextColumn("[bold green]doc2dict + agreement_config[/bold green]"),
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

            doc, sections = parse_one(idx, raw)
            doc["company"] = company
            doc["doc_type"] = doc_type
            doc_records.append(doc)
            for s in sections:
                s["idx"] = idx
                all_section_records.append(s)
                # Stream the JSONL line with idx ordered first for readability.
                jsonl_record = {
                    "idx": s["idx"],
                    **{k: v for k, v in s.items() if k != "idx"},
                }
                nodes_jsonl.write(
                    json.dumps(jsonl_record, ensure_ascii=False) + "\n"
                )

            progress.update(
                task,
                advance=1,
                description=(
                    f"idx={idx} {company[:24]:<24} "
                    f"{doc['parse_status']:<12} "
                    f"sections={doc['n_section_nodes']:>4} typed={doc['n_typed_headers']:>3} "
                    f"body_kb={doc['total_body_chars']/1024:>6.1f}"
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
    logger.info(
        f"wrote {nodes_jsonl_path}: {len(all_section_records)} JSONL lines "
        f"(streamed during parsing)"
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

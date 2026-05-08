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
#     "regex>=2024.11.6",
#     "beautifulsoup4>=4.12",
#     "lxml>=5.3",
# ]
# ///
"""Baseline parser using lexnlp's regex section detector — with span text.

NO customization on lexnlp itself. We call `section_spans_re(plain_text)` and
write what comes back, including the actual text content of each span.

Why a paragraph-preserving HTML→text conversion is required (and is not
"customization" of lexnlp): lexnlp's section regex matches against
line-anchored patterns (`(?<=\\n)Section\\s+...`). If the HTML strip merges
everything onto one line, lexnlp's detector reports near-zero sections.
The plain-text extraction here uses BeautifulSoup with newline injection
on block-level tags, then hands that text directly to lexnlp.

Outputs
-------
Three files written to --output-dir:

    parse_lexnlp_docs.parquet     One row per source document.
    parse_lexnlp_spans.parquet    One row per span, INCLUDING span_text.
    parse_lexnlp_spans.jsonl      ONE LINE PER PARSED UNIT, only 3 keys per line.

JSONL schema (the ONLY 3 keys per line)
---------------------------------------
    idx    int    Source corpus row index.
    level  int    lexnlp's relative `level` (1-indexed; lex assigns level 1
                  to the topmost heading hierarchy and increments downward).
                  doc2dict uses 0-indexed `depth` instead — not normalized
                  so each parser's view is preserved.
    span   str    The span's full text (lexnlp's `span_text`, which already
                  includes the heading inline because lex captures the
                  section break and the following content as one slice).
                  Strings longer than 4000 chars truncated to
                  `<head 3000>[TRUNCATED]<tail 1000>`.

Concatenating all `span` values for one `idx` in span_id order should
reconstruct the document's plain text — by lexnlp's design, every char of
the input belongs to exactly one span. Compare against the matching
reconstruction from doc2dict to find places where each parser is missing
or duplicating content.

The parquet contains FULL span text and ALL fields. The JSONL is the
inspection-ready shape; parquet is the source of truth.

Span fields
-----------
    span_id      : int    In-doc index (start-of-section order).
    start, end   : int    Char offsets in the doc's plain text.
    title        : str    The detected section title (or null).
    title_start, title_end : int
    level        : int    lexnlp's relative level (in-doc).
    abs_level    : int    lexnlp's absolute hierarchy level.
                          1=Appendix/Exhibit/Schedule/Part/Title;
                          3=Section; 4=Subsection; 5=Article;
                          6=ALL-CAPS heading; 7=numeric fallback;
                          8=alphabetic fallback; 9=parenthesized fallback.
    span_chars   : int    Length of span_text in chars.
    span_text    : str    The actual section content as lexnlp delimited it.

Lexnlp returns spans, not a tree. They DO NOT nest — adjacent spans are
literally adjacent slices of the plain-text string. So a "Section 5" span
contains its (a)(b)(c) children's text inline, AND there are also separate
spans for (a), (b), (c). This redundancy is built into lexnlp's output;
we write what we get.

Usage
-----
    uv run scripts/parse_lexnlp.py --limit 5
    HF_TOKEN=hf_xxx uv run scripts/parse_lexnlp.py \\
        --output-dir data/runs/lexnlp_baseline
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

_REPO_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_REPO_SRC) not in sys.path:
    sys.path.insert(0, str(_REPO_SRC))

from clause_extract.lexnlp_sections_regex import section_spans_re  # noqa: E402

CORPUS_URL = (
    "hf://datasets/arthrod/new3_results_master22017_274.59mb"
    "/data/train-00000-of-00001.parquet"
)

_HTML_OPEN = re.compile(r"<html[\s>]", re.IGNORECASE)
_HTML_CLOSE = re.compile(r"</html\s*>", re.IGNORECASE)
_IMG_TAG = re.compile(r"<img\b", re.IGNORECASE)
_TAG_STRIP = re.compile(r"<[^>]+>")

_BLOCK_TAGS = {
    "p", "div", "br", "hr",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "li", "ul", "ol",
    "tr", "td", "th",
    "table", "thead", "tbody", "tfoot",
    "section", "article", "header", "footer", "aside",
    "blockquote", "pre",
    "address",
}


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


# JSONL truncation: long fields get summarized for inspection-friendly output.
# Parquet keeps the full text — this only affects the JSONL writer.
_JSONL_TRUNCATE_THRESHOLD = 4000
_JSONL_TRUNCATE_HEAD = 3000
_JSONL_TRUNCATE_TAIL = 1000


def _truncate_for_jsonl(value: Any) -> Any:
    """If `value` is a string longer than the threshold, return
    `<head>[TRUNCATED]<tail>`. Otherwise return the value unchanged."""
    if not isinstance(value, str) or len(value) <= _JSONL_TRUNCATE_THRESHOLD:
        return value
    return (
        value[:_JSONL_TRUNCATE_HEAD]
        + "[TRUNCATED]"
        + value[-_JSONL_TRUNCATE_TAIL:]
    )


def html_to_plain_text(html: str) -> str:
    """Paragraph-preserving HTML → text. Required for lexnlp's line-anchored
    regex to find section boundaries."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "meta", "link"]):
        tag.decompose()
    for tag in soup.find_all(_BLOCK_TAGS):
        if tag.name == "br":
            tag.replace_with("\n")
        else:
            tag.append("\n")
    text = soup.get_text(separator="")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    text = text.replace("\xa0", " ")
    return text.strip()


def parse_one(idx: int, raw: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """One corpus row → (summary, list_of_span_records)."""
    size_kb = round(len(raw) / 1024, 2)
    base: dict[str, Any] = {
        "idx": idx,
        "size_kb": size_kb,
        "plain_text_kb": 0.0,
        "parse_status": None,
        "parse_error": None,
        "elapsed_ms": None,
        "n_spans": 0,
        "n_titled": 0,
        "max_abs_level": 0,
        "max_rel_level": 0,
        "total_span_chars": 0,
        "abs_level_counts_json": "{}",
    }

    html = extract_html(raw)
    if html is None:
        base["parse_status"] = "no_html_block"
        return base, []
    if is_image_only(html):
        base["parse_status"] = "image_only"
        return base, []

    try:
        plain = html_to_plain_text(html)
    except Exception as e:
        base["parse_status"] = "error"
        base["parse_error"] = f"plain_text_extraction: {type(e).__name__}: {e}"
        return base, []
    base["plain_text_kb"] = round(len(plain) / 1024, 2)

    t0 = time.perf_counter()
    try:
        spans = list(section_spans_re(plain))  # ← lexnlp's defaults; no overrides
    except Exception as e:
        base["parse_status"] = "error"
        base["parse_error"] = f"section_spans_re: {type(e).__name__}: {e}"
        base["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        return base, []
    elapsed = (time.perf_counter() - t0) * 1000
    base["elapsed_ms"] = round(elapsed, 2)

    span_records: list[dict[str, Any]] = []
    abs_counter: Counter[int] = Counter()
    total_chars = 0
    for span_id, sp in enumerate(spans):
        title = sp.title or None
        # sp.text is the actual content slice — that's what we want in the parquet
        span_text = sp.text if isinstance(sp.text, str) else plain[sp.start : sp.end]
        span_records.append({
            "span_id": span_id,
            "start": sp.start,
            "end": sp.end,
            "title": title,
            "title_start": sp.title_start,
            "title_end": sp.title_end,
            "level": sp.level,
            "abs_level": sp.abs_level,
            "span_chars": len(span_text),
            "span_text": span_text,
        })
        if title:
            base["n_titled"] += 1
        abs_counter[sp.abs_level] += 1
        total_chars += len(span_text)

    base["parse_status"] = "ok"
    base["n_spans"] = len(spans)
    base["max_abs_level"] = max(abs_counter.keys(), default=0)
    base["max_rel_level"] = max((sp.level for sp in spans), default=0)
    base["total_span_chars"] = total_chars
    base["abs_level_counts_json"] = json.dumps(
        {str(k): v for k, v in abs_counter.items()}
    )
    return base, span_records


def main(
    output_dir: Path = typer.Option(
        Path("data/runs/lexnlp_baseline"),
        help="Where to write parse_lexnlp_{docs,spans}.parquet.",
    ),
    limit: int = typer.Option(0, help="Parse only the first N rows. 0 = all."),
    no_truncate: bool = typer.Option(
        False,
        "--no-truncate",
        help=(
            "Disable JSONL truncation. By default, strings longer than "
            f"{_JSONL_TRUNCATE_THRESHOLD} chars are summarized to "
            f"<head {_JSONL_TRUNCATE_HEAD}>[TRUNCATED]<tail {_JSONL_TRUNCATE_TAIL}>. "
            "With this flag, JSONL spans are written full-length — useful "
            "when the JSONL feeds reconstruction-quality measurement."
        ),
    ),
    hf_token: str | None = typer.Option(
        None,
        envvar="HF_TOKEN",
        help="HuggingFace token. Reads /home/claude/.hf_token as fallback.",
    ),
) -> None:
    """Run baseline lexnlp regex section detector over the full corpus."""
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
    all_span_records: list[dict[str, Any]] = []

    # JSONL is written streamingly so a long-running corpus parse leaves a
    # readable partial file even if it crashes mid-loop. Parquet still
    # accumulates in memory and is written at the end (matches the existing
    # pattern; switching parquet to streaming would need a different writer).
    spans_jsonl_path = output_dir / "parse_lexnlp_spans.jsonl"
    with spans_jsonl_path.open("w", encoding="utf-8") as spans_jsonl, Progress(
        SpinnerColumn(),
        TextColumn("[bold magenta]lexnlp baseline[/bold magenta]"),
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

            doc, spans = parse_one(idx, raw)
            doc["company"] = company
            doc["doc_type"] = doc_type
            doc_records.append(doc)
            for sp in spans:
                sp["idx"] = idx
                all_span_records.append(sp)
                # Build the 3-key JSONL record. Skip records whose span_text
                # is empty (rare but possible at doc boundaries).
                span = sp.get("span_text") or ""
                if not span:
                    continue
                jsonl_record = {
                    "idx": sp["idx"],
                    "level": sp["level"],
                    "span": span if no_truncate else _truncate_for_jsonl(span),
                }
                spans_jsonl.write(
                    json.dumps(jsonl_record, ensure_ascii=False) + "\n"
                )

            progress.update(
                task,
                advance=1,
                description=(
                    f"idx={idx} {company[:24]:<24} "
                    f"{doc['parse_status']:<12} "
                    f"spans={doc['n_spans']:>4} body_kb={doc['total_span_chars']/1024:>6.1f}"
                ),
            )

    docs_df = pl.DataFrame(doc_records).select(
        pl.col("idx"),
        pl.col("company"),
        pl.col("doc_type"),
        pl.col("size_kb"),
        pl.col("plain_text_kb"),
        pl.col("parse_status"),
        pl.col("parse_error"),
        pl.col("elapsed_ms"),
        pl.col("n_spans"),
        pl.col("n_titled"),
        pl.col("max_abs_level"),
        pl.col("max_rel_level"),
        pl.col("total_span_chars"),
        pl.col("abs_level_counts_json"),
    )
    docs_path = output_dir / "parse_lexnlp_docs.parquet"
    docs_df.write_parquet(docs_path, compression="zstd")
    logger.info(f"wrote {docs_path}: {len(docs_df)} doc rows")

    spans_df = pl.DataFrame(all_span_records).select(
        pl.col("idx"),
        pl.col("span_id"),
        pl.col("start"),
        pl.col("end"),
        pl.col("title"),
        pl.col("title_start"),
        pl.col("title_end"),
        pl.col("level"),
        pl.col("abs_level"),
        pl.col("span_chars"),
        pl.col("span_text"),
    )
    spans_path = output_dir / "parse_lexnlp_spans.parquet"
    spans_df.write_parquet(spans_path, compression="zstd")
    logger.info(f"wrote {spans_path}: {len(spans_df)} span rows")
    truncation_note = (
        "no truncation (--no-truncate)"
        if no_truncate
        else f"strings >{_JSONL_TRUNCATE_THRESHOLD} chars truncated"
    )
    logger.info(
        f"wrote {spans_jsonl_path}: {len(all_span_records)} JSONL lines "
        f"(streamed during parsing; {truncation_note})"
    )

    status_counts = Counter(d["parse_status"] for d in doc_records)
    logger.info("=== status breakdown ===")
    for s, n in status_counts.most_common():
        logger.info(f"  {s:<15} {n:>4}")

    if status_counts.get("ok", 0):
        ok = [d for d in doc_records if d["parse_status"] == "ok"]
        n = len(ok)
        avg_spans = sum(d["n_spans"] for d in ok) / n
        avg_titled = sum(d["n_titled"] for d in ok) / n
        avg_body = sum(d["total_span_chars"] for d in ok) / n / 1024
        avg_abs = sum(d["max_abs_level"] for d in ok) / n
        avg_rel = sum(d["max_rel_level"] for d in ok) / n
        avg_ms = sum(d["elapsed_ms"] for d in ok) / n
        logger.info("=== ok-doc averages ===")
        logger.info(f"  avg spans:           {avg_spans:>7.1f}")
        logger.info(f"  avg titled spans:    {avg_titled:>7.1f}")
        logger.info(f"  avg body kb (sum of span text): {avg_body:>7.1f}")
        logger.info(f"  avg max abs_level:   {avg_abs:>7.2f}")
        logger.info(f"  avg max rel level:   {avg_rel:>7.2f}")
        logger.info(f"  avg parse time (ms): {avg_ms:>7.1f}")


if __name__ == "__main__":
    typer.run(main)

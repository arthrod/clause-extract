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
"""Baseline parser using lexnlp's regex section detector.

NO customization. NO custom hierarchy. NO inverted ordering. We call
`section_spans_re` directly on the document's plain text and write out
whatever lexnlp gives us.

The vendored module is at `src/clause_extract/lexnlp_sections_regex.py`
— a self-contained AGPLv3 port of the regex-only path of
`lexnlp.nlp.en.segments.sections` from arthrod/lexpredict-lexnlp.
The `SectionLevelParser.DEFAULT_SECTION_HIERARCHY` is used as-is — the
order is lexnlp's original 10-K-shaped ordering (Section above Article in
abs_level), which is what "no customization" means here.

Inputs
------
The 1066-row corpus at `arthrod/new3_results_master22017_274.59mb`. Same
extraction path as the doc2dict script — pull HTML out of the SEC SGML
envelope, then convert HTML → plain text via BeautifulSoup with paragraph
preservation (lexnlp's section detector relies on line boundaries).

Outputs
-------
Two parquet files written to --output-dir:

    parse_lexnlp_docs.parquet       One row per source document.
        idx                  : int
        company              : string
        doc_type             : string
        size_kb              : float
        plain_text_kb        : float    Plain-text size (for the lex input).
        parse_status         : string   ok | image_only | no_html_block | error.
        parse_error          : string
        elapsed_ms           : float
        n_spans              : int      Total section spans lexnlp returned.
        n_titled             : int      Spans with a parsed title.
        max_abs_level        : int      Highest abs_level seen across spans.
        max_rel_level        : int      Highest level (relative) seen.
        abs_level_counts_json: string   JSON map of abs_level → count.

    parse_lexnlp_spans.parquet      One row per span across all docs.
        idx                  : int
        span_id              : int      Stable in-doc index (start-of-section order).
        start                : int      Char offset in plain_text.
        end                  : int
        title                : string   The detected section title (or null).
        title_start          : int
        title_end            : int
        level                : int      lexnlp's relative level (in-doc).
        abs_level            : int      lexnlp's absolute hierarchy level
                                        (1 = Appendix/Exhibit/Schedule/Part/Title,
                                         3 = Section, 5 = Article, ...).
        span_chars           : int      Length of the section's text.

Notes
-----
- The plain-text extraction uses a paragraph-preserving converter: every
  block-level element ends in '\\n', so lexnlp's line-anchored regex can
  find section boundaries. Tag-stripping with '<[^>]+>' alone destroys
  paragraph boundaries and lex undercounts catastrophically.
- We do NOT load lexnlp's ML section classifier (the sklearn pickle).
  Per `docs/DECISIONS.md` §8, the regex-only path is what runs.

Usage
-----
    HF_TOKEN=hf_xxx uv run scripts/parse_lexnlp.py \\
        --output-dir data/runs/lexnlp_baseline \\
        --limit 1066

    HF_TOKEN=hf_xxx uv run scripts/parse_lexnlp.py --limit 5
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

# Make the vendored lexnlp module importable without installing the package
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

# Block-level tags whose end should produce a newline so that lex's
# line-anchored regex can find section boundaries
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
    last_close = None
    for m in _HTML_CLOSE.finditer(raw):
        last_close = m
    if last_close is None:
        return None
    return raw[m_open.start() : last_close.end()]


def is_image_only(html: str) -> bool:
    visible = _TAG_STRIP.sub(" ", html)
    visible = re.sub(r"\s+", " ", visible).strip()
    n_imgs = len(_IMG_TAG.findall(html))
    return len(visible) < 200 and n_imgs >= 1


def html_to_plain_text(html: str) -> str:
    """Convert HTML to plain text preserving paragraph boundaries.

    lexnlp's section detector matches against line-anchored patterns
    (`(?<=\\n)Section\\s+...`), so paragraphs MUST end with newlines for
    detection to work. A naive `<[^>]+>` strip merges everything into one
    line and the detector reports near-zero sections.
    """
    soup = BeautifulSoup(html, "lxml")

    # Strip script/style/meta noise
    for tag in soup(["script", "style", "noscript", "meta", "link"]):
        tag.decompose()

    # Insert newlines after block-level tags (and before <br>)
    for tag in soup.find_all(_BLOCK_TAGS):
        if tag.name == "br":
            tag.replace_with("\n")
        else:
            # Append a newline to the tag's tail
            tag.append("\n")

    text = soup.get_text(separator="")
    # Normalize whitespace: collapse runs of non-newline whitespace, dedupe blank lines
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    # Decode common HTML entities that bs4 sometimes leaves
    text = text.replace("\xa0", " ")
    return text.strip()


def parse_one(idx: int, raw: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Parse a single corpus row through the lexnlp regex section detector."""
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
        "abs_level_counts_json": "{}",
    }
    spans_out: list[dict[str, Any]] = []

    html = extract_html(raw)
    if html is None:
        base["parse_status"] = "no_html_block"
        return base, spans_out
    if is_image_only(html):
        base["parse_status"] = "image_only"
        return base, spans_out

    try:
        plain = html_to_plain_text(html)
    except Exception as e:
        base["parse_status"] = "error"
        base["parse_error"] = f"plain_text_extraction: {type(e).__name__}: {e}"
        return base, spans_out
    base["plain_text_kb"] = round(len(plain) / 1024, 2)

    t0 = time.perf_counter()
    try:
        # NO customization — bare `section_spans_re` call. The function uses
        # lexnlp's DEFAULT_SECTION_HIERARCHY internally with no overrides.
        spans = list(section_spans_re(plain))
    except Exception as e:
        base["parse_status"] = "error"
        base["parse_error"] = f"section_spans_re: {type(e).__name__}: {e}"
        base["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        return base, spans_out
    elapsed = (time.perf_counter() - t0) * 1000
    base["elapsed_ms"] = round(elapsed, 2)

    abs_counter: Counter[int] = Counter()
    for span_id, sp in enumerate(spans):
        title = sp.title or None
        spans_out.append({
            "span_id": span_id,
            "start": sp.start,
            "end": sp.end,
            "title": title,
            "title_start": sp.title_start,
            "title_end": sp.title_end,
            "level": sp.level,
            "abs_level": sp.abs_level,
            "span_chars": sp.end - sp.start,
        })
        if title:
            base["n_titled"] += 1
        abs_counter[sp.abs_level] += 1

    base["parse_status"] = "ok"
    base["n_spans"] = len(spans)
    base["max_abs_level"] = max(abs_counter.keys(), default=0)
    base["max_rel_level"] = max((sp.level for sp in spans), default=0)
    base["abs_level_counts_json"] = json.dumps(
        {str(k): v for k, v in abs_counter.items()}
    )
    return base, spans_out


def main(
    output_dir: Path = typer.Option(
        Path("data/runs/lexnlp_baseline"),
        help="Where to write parse_lexnlp_docs.parquet and parse_lexnlp_spans.parquet.",
    ),
    limit: int = typer.Option(0, help="Parse only the first N rows. 0 = all."),
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
    all_span_records: list[dict[str, Any]] = []

    with Progress(
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

            progress.update(
                task,
                advance=1,
                description=(
                    f"idx={idx} {company[:24]:<24} "
                    f"status={doc['parse_status']:<12} "
                    f"spans={doc['n_spans']:>4} titled={doc['n_titled']:>4}"
                ),
            )

    # Write outputs
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
    )
    spans_path = output_dir / "parse_lexnlp_spans.parquet"
    spans_df.write_parquet(spans_path, compression="zstd")
    logger.info(f"wrote {spans_path}: {len(spans_df)} span rows")

    # Headline summary
    status_counts = Counter(d["parse_status"] for d in doc_records)
    logger.info("=== status breakdown ===")
    for s, n in status_counts.most_common():
        logger.info(f"  {s:<15} {n:>4}")

    if status_counts.get("ok", 0):
        ok = [d for d in doc_records if d["parse_status"] == "ok"]
        avg_spans = sum(d["n_spans"] for d in ok) / len(ok)
        avg_titled = sum(d["n_titled"] for d in ok) / len(ok)
        avg_abs = sum(d["max_abs_level"] for d in ok) / len(ok)
        avg_rel = sum(d["max_rel_level"] for d in ok) / len(ok)
        avg_ms = sum(d["elapsed_ms"] for d in ok) / len(ok)
        logger.info("=== ok-doc averages ===")
        logger.info(f"  avg spans:           {avg_spans:>7.1f}")
        logger.info(f"  avg titled spans:    {avg_titled:>7.1f}")
        logger.info(f"  avg max abs_level:   {avg_abs:>7.2f}")
        logger.info(f"  avg max rel level:   {avg_rel:>7.2f}")
        logger.info(f"  avg parse time (ms): {avg_ms:>7.1f}")


if __name__ == "__main__":
    typer.run(main)

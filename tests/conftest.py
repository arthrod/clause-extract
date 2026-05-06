"""Test fixtures and pytest configuration."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def hf_token() -> str:
    """The HuggingFace token, from env var or local file.

    Skips the test cleanly if no token is available — the CI for non-SOT
    tests must not require network or auth.
    """
    token = os.environ.get("HF_TOKEN")
    if token:
        return token
    fallback = Path("/home/claude/.hf_token")
    if fallback.exists():
        return fallback.read_text().strip()
    pytest.skip("HF_TOKEN not set and no fallback file; skipping SOT/corpus test")


@pytest.fixture(scope="session")
def sot_dataset(hf_token: str):
    """Load the source-of-truth dataset rows.

    Returns a list[dict], one per SOT document, with `idx`, `clause_ledger_json`,
    `extraction_notes`, etc.
    """
    pl = pytest.importorskip("polars")
    url = "hf://datasets/arthrod/clause-prob-source-of-truth/data/train-00000-of-00001.parquet"
    df = pl.scan_parquet(url, storage_options={"token": hf_token}).collect()
    rows = []
    for i in range(len(df)):
        r = df.row(i, named=True)
        r["clause_ledger"] = json.loads(r["clause_ledger_json"])
        rows.append(r)
    return rows


@pytest.fixture(scope="session")
def sot_clauses(sot_dataset):
    """Flatten SOT to a list of (doc_idx, clause_dict) tuples for parametrized tests."""
    out = []
    for r in sot_dataset:
        for c in r["clause_ledger"]:
            out.append((r["idx"], c))
    return out

"""Validation gate 1: canonical_id_parser round-trips 973/973 SOT clauses.

For every clause_id in every SOT ledger:
  - Parse it: parse_clause_id(cid) returns a non-None segment list.
  - Round-trip: _segments_to_clause_id(segs) reconstructs exactly cid.
  - Parent: derive_parent_id(cid) is either None (root) or another clause_id
    present in the same document's ledger.

The whole gate is one parametrized test, one assertion per clause, so failures
report which specific clause_id broke the rule.

Marked `sot` (network-fetching). Use `pytest -m sot` to run; default test runs
skip it.
"""
from __future__ import annotations

import pytest

from clause_extract.canonical_id_parser import (
    _segments_to_clause_id,
    derive_parent_id,
    parse_clause_id,
)


@pytest.mark.sot
def test_every_sot_clause_parses_and_round_trips(sot_dataset):
    """Aggregate gate: report 100% pass over all SOT clauses, with a breakdown
    of any failures so a diff is actionable."""
    total = 0
    n_unparseable: list[tuple[int, str]] = []
    n_rt_fail: list[tuple[int, str, str]] = []
    n_parent_missing: list[tuple[int, str, str]] = []
    n_root = 0
    n_consistent = 0

    for r in sot_dataset:
        idx = r["idx"]
        ledger = r["clause_ledger"]
        ids_set = {c["clause_id"] for c in ledger}

        for c in ledger:
            total += 1
            cid = c["clause_id"]
            segs = parse_clause_id(cid)
            if segs is None:
                n_unparseable.append((idx, cid))
                continue
            rebuilt = _segments_to_clause_id(segs)
            if rebuilt != cid:
                n_rt_fail.append((idx, cid, rebuilt))
                continue
            parent = derive_parent_id(cid)
            if parent is None:
                n_root += 1
            elif parent not in ids_set:
                n_parent_missing.append((idx, cid, parent))
            else:
                n_consistent += 1

    # The gate: zero failures across all categories.
    failures = []
    if n_unparseable:
        failures.append(f"unparseable: {len(n_unparseable)} (first 5: {n_unparseable[:5]})")
    if n_rt_fail:
        failures.append(f"round-trip-fail: {len(n_rt_fail)} (first 5: {n_rt_fail[:5]})")
    if n_parent_missing:
        failures.append(
            f"parent-missing: {len(n_parent_missing)} "
            f"(first 5: {n_parent_missing[:5]})"
        )

    assert not failures, (
        f"SOT validation failed for {total} total clauses:\n  "
        + "\n  ".join(failures)
    )

    # Sanity: counts add up
    assert n_root + n_consistent == total, (
        f"Internal accounting error: {n_root=} + {n_consistent=} != {total=}"
    )


@pytest.mark.sot
def test_every_sot_clause_parametrized(sot_clauses):
    """Per-clause assertion. Provides a nicer failure report when one breaks
    (you see the failing clause_id directly in the test name)."""
    for doc_idx, c in sot_clauses:
        cid = c["clause_id"]
        segs = parse_clause_id(cid)
        assert segs is not None, f"idx={doc_idx} clause_id={cid!r}: failed to parse"
        rebuilt = _segments_to_clause_id(segs)
        assert rebuilt == cid, (
            f"idx={doc_idx} clause_id={cid!r}: round-trip produced {rebuilt!r}"
        )

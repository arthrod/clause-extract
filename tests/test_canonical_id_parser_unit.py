"""Unit tests for canonical_id_parser. No network. Covers every form in
`docs/SCHEMA.md` "Recognized canonical-path forms" table, plus regression
cases from the design session."""
from __future__ import annotations

import pytest

from clause_extract.canonical_id_parser import (
    _segments_to_clause_id,
    derive_depth,
    derive_parent_id,
    parse_clause_id,
)


# Each row: (clause_id, expected_segments_kinds, expected_parent, expected_depth)
PARSER_CASES = [
    # Envelope keyword
    ("exhibit10.26", ["exhibit"], None, 0),
    ("schedule.A", ["schedule"], None, 0),
    ("article5", ["article"], None, 0),
    ("annex1", ["annex"], None, 0),
    # Bare numeric + paren
    ("1", ["section"], None, 0),
    ("13", ["section"], None, 0),
    ("13(a)", ["section", "subsection"], "13", 1),
    ("13(a)(v)", ["section", "subsection", "sub_subsection"], "13(a)", 2),
    # Roman primary
    ("I", ["article_roman"], None, 0),
    ("II", ["article_roman"], None, 0),
    ("IX(a)", ["article_roman", "subsection"], "IX", 1),
    ("IV.2.iii", ["article_roman", "section_after_roman", "section_roman"], "IV.2", 2),
    ("VII.3", ["article_roman", "section_after_roman"], "VII", 1),
    # Section-prefixed
    ("section1", ["section@"], None, 0),
    ("section2(b)(1)", ["section@", "subsection", "sub_subsection"], "section2(b)", 2),
    ("section8(3)", ["section@", "subsection"], "section8", 1),
    # Document-namespace
    ("la.1.1", ["subdocument", "section"], None, 1),
    ("cl.7.4", ["subdocument", "section"], None, 1),
    ("ppa.term_duration", ["subdocument", "topical"], None, 1),
    ("att1.epirp", ["subdocument", "topical"], None, 1),
    ("msw_concession.parties", ["subdocument", "topical"], None, 1),
    ("ac.13(a)", ["subdocument", "section", "subsection"], "ac.13", 2),
    # Topical roots
    ("preamble", ["topical"], None, 0),
    ("closing", ["topical"], None, 0),
    ("vesting", ["topical"], None, 0),
    ("taxes", ["topical"], None, 0),
    ("death_or_disability", ["topical"], None, 0),
    # Recital encodings
    ("recital_A", ["recital@_"], None, 0),
    ("recital.A", ["recital@."], None, 0),
    ("whereas.1", ["whereas@."], None, 0),
    # Definition entries
    ("def(a)", ["subdocument_nodot", "subsection"], None, 1),
    ("def(j)", ["subdocument_nodot", "subsection"], None, 1),
    # Roman with topical tail
    ("XI_dispute", ["article_roman", "topical_under"], None, 1),
    ("XI_misc", ["article_roman", "topical_under"], None, 1),
    # Dot-letter tail
    ("3.01(f).A", ["section", "subsection", "sub_subsection_dotted"], "3.01(f)", 2),
]


@pytest.mark.parametrize(("cid", "kinds", "parent", "depth"), PARSER_CASES)
def test_parses_form(cid: str, kinds: list[str], parent: str | None, depth: int):
    segs = parse_clause_id(cid)
    assert segs is not None, f"{cid!r}: failed to parse"
    assert [s[0] for s in segs] == kinds, f"{cid!r}: wrong segment kinds"
    assert derive_parent_id(cid) == parent, f"{cid!r}: wrong parent"
    assert derive_depth(cid) == depth, f"{cid!r}: wrong depth"


@pytest.mark.parametrize(("cid", "kinds", "parent", "depth"), PARSER_CASES)
def test_round_trips(cid: str, kinds: list[str], parent: str | None, depth: int):
    segs = parse_clause_id(cid)
    assert segs is not None
    rebuilt = _segments_to_clause_id(segs)
    assert rebuilt == cid, f"{cid!r} → {segs} → {rebuilt!r}"


def test_garbage_returns_none():
    """Truly unparseable inputs return None, not a crash."""
    for bad in ["", "!@#$", "Article", "Section without number"]:
        assert parse_clause_id(bad) is None, f"{bad!r} should be unparseable"


def test_subdocument_only_parent_is_root():
    """When the parent path reduces to a bare subdocument scope, parent is None."""
    # la.1's parent would be 'la.' (bare subdocument), which is a scope, not a record.
    assert derive_parent_id("la.1") is None
    assert derive_parent_id("ppa.term_duration") is None
    assert derive_parent_id("def(a)") is None
    # But ac.13(a) has parent ac.13 (real clause)
    assert derive_parent_id("ac.13(a)") == "ac.13"


def test_roman_with_topical_under_is_root():
    """XI_dispute's "parent" XI doesn't exist as a clause record."""
    assert derive_parent_id("XI_dispute") is None
    assert derive_parent_id("XI_misc") is None

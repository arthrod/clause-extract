"""
doc2dict mapping_dict config for EX-10 exhibit agreements.

Derived from the SectionLevelParser.DEFAULT_SECTION_HIERARCHY in
arthrod/lexpredict-lexnlp:lexnlp/nlp/en/segments/sections.py with three changes:

  1. Hierarchy ordering inverted to reflect agreement convention
     (Article > Section > Subsection), instead of 10-K convention
     (Part > Item; Section above Article).

  2. All patterns ANCHORED both at start and end (^...$) to prevent
     body-prose false positives. doc2dict applies re.match() with
     no flags; re.match anchors the start; we add $ to require the
     stripped header text to match in full.

  3. Capture groups preserved so doc2dict produces useful
     standardized_titles like 'article1', 'section2.3', 'exhibita'.

doc2dict consumes these via the {int_level: [{name, regex}, ...]} shape.
Lower integer = higher in hierarchy.
"""

from __future__ import annotations

import copy
from typing import Any

from doc2dict.mapping_dicts.standard_config import STANDARD_CONFIG


# ---------------------------------------------------------------------------
# Per-level patterns
# ---------------------------------------------------------------------------

# Level 0 — top of the agreement body and parallel attachments.
#
# In EX-10 contracts, the conventional top-level structural unit is ARTICLE.
# Schedules, Exhibits, Appendices, and Annexes also sit at the top level
# but as parallel attachments (often after the signature page).
#
# We list ARTICLE first and use word-only capture: roman, arabic, or
# spelled-out English numerals. The trailing "[-–—:.]?\s*$" allows headers
# like "ARTICLE I", "ARTICLE 1.", "ARTICLE 1 - GENERAL" — while the
# explicit $ blocks "article 7 of this agreement" body prose.
# Common trailing-title pattern for level-0 / level-1 headers.
#
# After the number we accept either end-of-string OR a clean separator
# (colon, en/em dash, hyphen) followed by up to 200 chars of
# descriptive text (an inline title like "ARTICLE 1: PURPOSE" or
# "Article 1 - The line of credit").
#
# Two design choices that prevent body-prose false positives:
#
# 1. Period is NOT in the separator set. With the regex engine able to
#    backtrack a digit capture, "section 7.2, section 8.1, ..." can
#    re-match as section="7" + separator="." + body="2, section 8.1...".
#    Period as separator is rare in modern agreements anyway.
#
# 2. A negative lookahead (?![\d,]) after the number capture stops the
#    engine from backtracking into a partial-digit capture when the
#    full match fails. This is the same defense, belt-and-suspenders.
#
# A comma after the number does NOT qualify as a separator — that is
# precisely how survival-clause body prose ("Section 7.2, Section 8.1,
# Article 14 ...") fooled the prior pilot's unanchored regex.
#
# Updated 50-doc pilot (v3) findings forced two further changes:
#
# 1. A `\.?` slot AFTER the lookahead, BEFORE the trailing group, to
#    handle the terminator period that follows the number ("Article 1.",
#    "Section 2.1.", "Article 1.1. — title"). The period is structural,
#    not a separator, so it stays out of the separator set.
#
# 2. _TRAILING accepts whitespace as a separator alternative. Many real
#    agreements write "Section 1.1 Definitions" with no colon/dash —
#    just a space between the number and title text. Collapsing the
#    separator set into a single `[\s:\-–—]+` character class allows
#    space-separated titles while still rejecting comma constructions
#    (the lookahead and the absence of comma from the set both hold).
#    Title bound bumped 200→500 to handle longer titles seen in v3.
#    `[\s\S]{0,500}` (rather than `.{0,500}`) so embedded newlines in
#    titles like "ARTICLE III – ORDER\nPLACEMENT" are accepted; in
#    Python's default re, `.` does not match `\n`.
_LOOKAHEAD = r"(?![\d,])"
_TRAILING = r"(?:[\s\:\-–—]+[\s\S]{0,500})?\s*$"

# Level 0 — Article (agreement-level divisions)
LEVEL_0_PATTERNS = [
    {
        "name": "article",
        "regex": (
            r"^article\s+"
            r"([ivxlcdm]+|\d+(?:\.\d+)*|"
            r"one|two|three|four|five|six|seven|eight|nine|ten|"
            r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|"
            r"seventeen|eighteen|nineteen|twenty)"
            + _LOOKAHEAD + r"\.?" + _TRAILING + r"$"
        ),
    },
]

# Level 1 — Exhibit/Schedule/Appendix/Annex (party/exhibit metadata) + Section.
#
# According to the rubric, Level 1 contains:
# - Party/exhibit metadata (e.g., "EXHIBIT 10.25", "ULURU Inc.")
# - WITNESSETH/WHEREAS recitals (captured as unstructured)
# - Signature block (captured as unstructured)
# - Numbered Sections (moved from Level 1 per rubric)
LEVEL_1_PATTERNS = [
    {
        "name": "exhibit",
        "regex": r"^exhibit\s+([a-z0-9][a-z0-9\-\.]*)" + _LOOKAHEAD + r"\.?" + _TRAILING + r"$",
    },
    {
        "name": "schedule",
        "regex": r"^schedule\s+([a-z0-9][a-z0-9\-\.]*)" + _LOOKAHEAD + r"\.?" + _TRAILING + r"$",
    },
    {
        "name": "appendix",
        "regex": r"^appendix\s+([a-z0-9][a-z0-9\-\.]*)" + _LOOKAHEAD + r"\.?" + _TRAILING + r"$",
    },
    {
        "name": "annex",
        "regex": r"^annex\s+([a-z0-9][a-z0-9\-\.]*)" + _LOOKAHEAD + r"\.?" + _TRAILING + r"$",
    },
    {
        "name": "section",
        "regex": r"^section\s+(\d+(?:\.\d+)*)" + _LOOKAHEAD + r"\.?" + _TRAILING + r"$",
    },
    # Some agreements also use "Title X" or "Part X" mid-document for
    # major divisions. lexnlp groups these with Article at abs_level 1;
    # we put Title/Part one rung below Article since in EX-10s when
    # they appear, they're usually inside an Article.
    {
        "name": "part",
        "regex": r"^part\s+([ivxlcdm]+|\d+)" + _LOOKAHEAD + r"\.?" + _TRAILING + r"$",
    },
    {
        "name": "title",
        "regex": r"^title\s+([ivxlcdm]+|\d+)" + _LOOKAHEAD + r"\.?" + _TRAILING + r"$",
    },
]

# Level 2 — Subsection / Subtitle.
LEVEL_2_PATTERNS = [
    {
        "name": "subsection",
        "regex": r"^subsection\s+([\d.]+)" + _LOOKAHEAD + r"\.?" + _TRAILING + r"$",
    },
    {
        "name": "subtitle",
        "regex": r"^subtitle\s+([ivxlcdm]+|\d+|[a-z])" + _LOOKAHEAD + r"\.?" + _TRAILING + r"$",
    },
]


AGREEMENT_LEVELS: dict[int, list[dict[str, str]]] = {
    0: LEVEL_0_PATTERNS,
    1: LEVEL_1_PATTERNS,
    2: LEVEL_2_PATTERNS,
}


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------


def make_agreement_config(extra_levels: dict[int, list[dict[str, str]]] | None = None) -> dict[str, Any]:
    """Return a deep-copied STANDARD_CONFIG with EX-10 levels merged in.

    Caller can pass `extra_levels` to add patterns at additional depths
    (e.g. for definition lists, parties, signatures) without mutating
    the shared module-level constants.
    """
    cfg = copy.deepcopy(STANDARD_CONFIG)
    levels = copy.deepcopy(AGREEMENT_LEVELS)
    if extra_levels:
        for k, patterns in extra_levels.items():
            levels.setdefault(k, []).extend(patterns)
    cfg["levels"] = levels
    return cfg


__all__ = [
    "AGREEMENT_LEVELS",
    "LEVEL_0_PATTERNS",
    "LEVEL_1_PATTERNS",
    "LEVEL_2_PATTERNS",
    "make_agreement_config",
]

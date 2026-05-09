"""Expanded canonical clause_id parser.

Adds support beyond the original decimal+paren rule:
  - Roman primary segments:    'I', 'II', 'IX(a)', 'IV.2.iii'
  - Namespace prefixes:        'la.1.1', 'cl.7.4', 'ppa.term_duration', 'att1.epirp', 'msw_concession.parties'
  - 'section'-prefixed:        'section1', 'section2(b)(1)'
  - Topical-name roots:        'preamble', 'closing', 'recitals_topical', 'agreement_heading'
  - Recital encodings:         'recital_A', 'recital.A', 'whereas.1'

The output of `parse_clause_id` is a list of (kind, identifier) segments.
Drop the last segment to derive the parent (the rule that already validated
100% on canonical-form clauses). The drop-last rule continues to apply to
all extended forms.

Reconstruction must be EQUIVALENCE-PRESERVING — i.e. parse(reconstruct(parse(cid)))
must produce a clause_id whose parent-chain matches `cid`'s parent-chain in
the original ledger. This means we keep the optional 'section'/'recital'/
'whereas' keyword prefix on output, since those are how the human encoded
those IDs in the ledger.
"""
from __future__ import annotations

import re

# Match keyword-prefixed envelope IDs: exhibit10.26, schedule.a, article5
_ENVELOPE_KW = r"(article|exhibit|schedule|appendix|annex)"
_ENVELOPE_RE = re.compile(rf"^{_ENVELOPE_KW}([\w\.\-]+)$", re.IGNORECASE)

# Numeric (with optional decimal continuation): 1, 5, 5.01, 13.4
_NUM_RE = re.compile(r"\d+(?:\.\d+)*")

# Roman numeral block: I, II, IV, IX
_ROMAN_RE = re.compile(r"[IVXLCDM]+", re.IGNORECASE)

# Lower-case roman / arabic / single letter inside parens: (a), (i), (1)
_PAREN_RE = re.compile(r"\(([^)]+)\)")

# Inline keywords: section, recital, whereas. Followed by `_`, `.`, or directly
# the identifier (e.g. 'section1'). 'article' is excluded because it conflicts
# with the article_roman path.
_INLINE_KW_RE = re.compile(r"^(section|recital|whereas)([_\.])?(\d.*|[A-Za-z].*)$", re.IGNORECASE)

# A doc-namespace prefix: lowercase letters/underscore/digit, followed by '.'.
# IMPORTANT: must NOT match the inline keywords above. We add a negative
# lookahead for those.
_DOCPREFIX_RE = re.compile(r"^(?!(section|recital|whereas|article|exhibit|schedule|appendix|annex)\.)([a-z][a-z0-9_]+)\.")

TOPICAL_ROOT_NAMES = {
    "preamble", "closing", "recitals", "recitals_topical", "title_block",
    "cover_sheet_preamble", "second_title_block", "agreement_topical",
    "agreement_heading", "important_notice", "miscellaneous", "definitions_topical",
    "methodology_title", "general_rate_making", "rate_making_procedures",
    "cost_centers", "certain_cost_center_allocations",
    "nonqualified_stock_option", "vesting", "termination", "death_or_disability",
    "termination_for_cause_recoupment", "change_of_control", "notice_of_exercise",
    "form_of_payment", "taxes", "transfer_of_option", "stockholder_rights",
    "no_right_to_continued_employment", "applicable_law_and_arbitration",
    "delivery_of_documents", "amendment", "the_plan_and_other_agreements",
    "restrictions_on_exercise", "restrictions_on_resale_company_policies",
    "effective_date", "eligibility", "compensation", "payment_timing_and_form",
    "prior_to_nav_threshold", "following_nav_threshold",
    "terms_conditions_restricted_stock", "proration",
    "airport_costs", "airport_credits", "pfc_revenues", "reduction_rates_charges",
    "concourse_a_baggage", "attachment_i",
    "service_confirmation_letter", "affirmation",
}


def _split_paren_tail(rest: str) -> tuple[list[str], str]:
    out = []
    while True:
        m = re.match(r"^\(([^)]+)\)", rest)
        if not m: break
        out.append(m.group(1))
        rest = rest[m.end():]
    return out, rest


def _kind_for_paren_token(token: str, depth_in_parens: int) -> str:
    return ["subsection", "sub_subsection", "sub_sub_subsection"][min(depth_in_parens, 2)]


def parse_clause_id(cid: str) -> list[tuple[str, str]] | None:
    if not cid: return None

    # Topical root names — single-segment paths
    if cid in TOPICAL_ROOT_NAMES:
        return [("topical", cid)]
    if re.fullmatch(r"[a-z][a-z_]+", cid):
        return [("topical", cid)]

    # Envelope keyword (exhibit, article, schedule, annex, appendix)
    m = _ENVELOPE_RE.match(cid)
    if m:
        return [(m.group(1).lower(), m.group(2))]

    # 'def(a)', 'def(b)' — 'def' is a 3-letter section-equivalent keyword used
    # in idx=137 for definition entries. Treat the bare 'def' prefix as a
    # subdocument scope, so def(a)'s parent is None (root).
    m = re.match(r"^def(\(.*\))$", cid)
    if m:
        rest = m.group(1)
        parens, leftover = _split_paren_tail(rest)
        if leftover: return None
        segs = [("subdocument_nodot", "def")]
        for i, p in enumerate(parens):
            segs.append((_kind_for_paren_token(p, i), p))
        return segs

    # Inline keywords (section1, recital_A, recital.A, whereas.1, section2(b)(1))
    # MUST run BEFORE doc-prefix matching because 'recital.A' and 'whereas.1'
    # would otherwise look like subdoc namespaces.
    m = _INLINE_KW_RE.match(cid)
    if m:
        keyword = m.group(1).lower()
        sep = m.group(2) or ""
        rest = m.group(3)
        # If rest is a bare letter or bare numeric, build a single-segment
        # clause directly. This handles 'recital_A', 'whereas.1', 'recital.A'.
        if re.fullmatch(r"\d+", rest) or re.fullmatch(r"[A-Za-z]", rest):
            head_kind = f"{keyword}@{sep}" if keyword in ("recital", "whereas") else f"section@{sep}"
            return [(head_kind, rest)]
        # Otherwise parse the rest structurally and rebrand the head.
        sub = parse_clause_id(rest)
        if sub is None:
            return None
        head_kind = f"{keyword}@{sep}" if keyword in ("recital", "whereas") else f"section@{sep}"
        new_head = (head_kind, sub[0][1])
        return [new_head] + sub[1:]

    # Doc-namespace prefix
    m = _DOCPREFIX_RE.match(cid)
    if m:
        prefix = m.group(2)
        rest = cid[len(prefix) + 1:]
        if not rest:
            return [("subdocument", prefix)]
        if re.fullmatch(r"[a-z][a-z0-9_]+", rest):
            return [("subdocument", prefix), ("topical", rest)]
        sub = parse_clause_id(rest)
        if sub is None: return None
        return [("subdocument", prefix)] + sub

    # Roman numeral primary with underscore-topical tail (XI_dispute, XI_misc)
    m = re.match(r"^([IVXLCDM]+)_([a-z][a-z0-9_]*)$", cid)
    if m:
        return [("article_roman", m.group(1)), ("topical_under", m.group(2))]

    # Roman numeral primary
    m = re.match(r"^([IVXLCDM]+)([\.\(].*)?$", cid)
    if m:
        roman = m.group(1)
        rest = m.group(2) or ""
        segs = [("article_roman", roman)]
        while rest.startswith("."):
            rest = rest[1:]
            n = re.match(r"^(\d+|[ivxlcdm]+)(.*)$", rest)
            if not n: return None
            tok = n.group(1)
            rest = n.group(2)
            kind = "section_roman" if re.fullmatch(r"[ivxlcdm]+", tok) else "section_after_roman"
            segs.append((kind, tok))
        parens, leftover = _split_paren_tail(rest)
        for i, p in enumerate(parens):
            segs.append((_kind_for_paren_token(p, i), p))
        if leftover: return None
        return segs

    # Lead numeric (5, 5.01)
    m = _NUM_RE.match(cid)
    if m:
        num = m.group(0)
        rest = cid[len(num):]
        segs = [("section", num)]
        parens, leftover = _split_paren_tail(rest)
        for i, p in enumerate(parens):
            segs.append((_kind_for_paren_token(p, i), p))
        # Trailing .X where X is a letter or roman (e.g., '3.01(f).A' → A is sub_subsection
        # using DOT-letter convention rather than paren convention).
        m_tail = re.match(r"^\.([A-Za-z]|[ivxlcdm]+)$", leftover, re.IGNORECASE)
        if m_tail:
            depth_in_parens = sum(1 for s in segs if "subsection" in s[0])
            kind = ["subsection_dotted", "sub_subsection_dotted", "sub_sub_subsection_dotted"][min(depth_in_parens, 2)]
            segs.append((kind, m_tail.group(1)))
            leftover = ""
        if leftover: return None
        return segs

    return None


def _segments_to_clause_id(segs: list[tuple[str, str]]) -> str:
    """Inverse of parse_clause_id. Round-trips on every form supported."""
    out = ""
    for i, (kind, ident) in enumerate(segs):
        if kind in ("article", "exhibit", "schedule", "appendix", "annex"):
            out = f"{kind}{ident}"
        elif kind == "subdocument":
            out = f"{out}{ident}." if out else f"{ident}."
        elif kind == "subdocument_nodot":
            out = f"{out}{ident}" if out else f"{ident}"
        elif kind.startswith("section@"):
            sep = kind.split("@", 1)[1]
            out += f"section{sep}{ident}"
        elif kind.startswith("recital@"):
            sep = kind.split("@", 1)[1]
            out += f"recital{sep}{ident}"
        elif kind.startswith("whereas@"):
            sep = kind.split("@", 1)[1]
            out += f"whereas{sep}{ident}"
        elif kind == "section":
            out += ident
        elif kind == "section_roman":
            out += "." + ident
        elif kind == "section_after_roman":
            out += "." + ident
        elif kind == "article_roman":
            out += ident
        elif kind in ("recital", "whereas"):
            # No separator memo — default to '_' for recital, '.' for whereas
            sep = "_" if kind == "recital" else "."
            out += f"{kind}{sep}{ident}"
        elif kind in ("subsection", "sub_subsection", "sub_sub_subsection"):
            out += f"({ident})"
        elif kind in ("subsection_dotted", "sub_subsection_dotted", "sub_sub_subsection_dotted"):
            out += f".{ident}"
        elif kind == "topical_under":
            out += "_" + ident
        elif kind == "topical":
            if not out:
                out = ident
            elif out.endswith("."):
                out = out + ident
            else:
                out = out + "." + ident
        else:
            out += ident
    return out


def derive_parent_id(cid: str) -> str | None:
    """Return parent clause_id by dropping the last path segment, or None for root.

    Special case: if the parent path consists ONLY of a single subdocument
    segment (e.g. 'cs.', 'la.', 'ppa.'), return None. Subdocuments are
    contextual scopes, not clause records — they aren't materialized in
    ledgers. The structural parent of 'la.1' is the document root, not a
    synthetic 'la.' record.
    """
    segs = parse_clause_id(cid)
    if not segs or len(segs) <= 1: return None
    parent_segs = segs[:-1]
    # Subdocument-only parent → root (subdocuments are scopes, not clause records)
    if len(parent_segs) == 1 and parent_segs[0][0] in ("subdocument", "subdocument_nodot"):
        return None
    # Roman-numeral-only parent → root IF the original was roman+topical
    # (XI_dispute's "parent" XI doesn't exist as a clause — it's a roman scope label)
    if (len(parent_segs) == 1 and parent_segs[0][0] == "article_roman"
            and segs[-1][0] == "topical_under"):
        return None
    return _segments_to_clause_id(parent_segs)


def derive_depth(cid: str) -> int | None:
    segs = parse_clause_id(cid)
    if segs is None: return None
    return len(segs) - 1


__all__ = ["parse_clause_id", "derive_parent_id", "derive_depth", "_segments_to_clause_id"]

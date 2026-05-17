#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["typer>=0.15"]
# ///
"""Emit the per-turn prompt for opencode.

Each turn opens with fresh context and gets exactly this prompt. It includes:

  - The level-tuning rubric.
  - The current target idx.
  - The source-of-truth excerpt (cleaned text, capped) for that idx.
  - The current parser output (level + span head) for that idx.
  - The full list of frozen idxs whose output must NOT regress.
  - The mechanical commands the agent should call.

Read by `turn.sh` and piped into `opencode run`.

Usage
-----
    uv run scripts/level_loop/prompt.py        # print prompt to stdout
    uv run scripts/level_loop/prompt.py > /tmp/prompt.txt
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer

REPO = Path(__file__).resolve().parents[2]
DATA = REPO / "data" / "auto_parse"
STATE_PATH = DATA / "level_freeze" / "state.json"
SOURCE_OF_TRUTH = DATA / "parse_source_of_truth.jsonl"
NODES_JSONL = DATA / "parse_doc2dict_with_config_nodes.jsonl"
FROZEN_DIR = DATA / "level_freeze" / "frozen"
ATTEMPTS_DIR = DATA / "level_freeze" / "attempts"

# How much source-of-truth text to inline. The agent can read the file
# directly for more, but we want the core structure visible in-prompt.
_SOT_HEAD = 6000
_SOT_TAIL = 2000

# Span preview length when listing current parser output.
_SPAN_PREVIEW = 240


def load_state() -> dict:
    return json.loads(STATE_PATH.read_text())


def load_jsonl(path: Path, idx: int | None = None) -> list[dict]:
    out: list[dict] = []
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if idx is None or rec.get("idx") == idx:
            out.append(rec)
    return out


def excerpt_sot(idx: int) -> str:
    for rec in load_jsonl(SOURCE_OF_TRUTH):
        if rec.get("idx") == idx:
            text = rec.get("span_clean") or ""
            if len(text) <= _SOT_HEAD + _SOT_TAIL + 50:
                return text
            return text[:_SOT_HEAD] + "\n\n[... source-of-truth truncated for prompt ...]\n\n" + text[-_SOT_TAIL:]
    return f"(no source-of-truth row for idx={idx})"


def render_current_output(idx: int) -> str:
    records = load_jsonl(NODES_JSONL, idx=idx)
    if not records:
        return f"(no parser output yet for idx={idx} — run the parser first)"
    lines = []
    for r in records:
        span = r.get("span", "")
        if len(span) > _SPAN_PREVIEW:
            span = span[:_SPAN_PREVIEW].rstrip() + " …"
        span = span.replace("\n", " \\n ")
        lines.append(f"  level={r.get('level')} | {span}")
    return "\n".join(lines)


def render_frozen_summary(frozen: list[int]) -> str:
    if not frozen:
        return "(none yet)"
    lines = []
    for i in frozen:
        path = FROZEN_DIR / f"idx_{i}.jsonl"
        if not path.exists():
            lines.append(f"  idx={i}: MISSING file at {path}")
            continue
        records = load_jsonl(path)
        levels = [r.get("level") for r in records]
        lines.append(f"  idx={i}: {len(records)} records, levels={levels}")
    return "\n".join(lines)


def load_attempts(idx: int) -> list[dict]:
    """Load prior failed-attempt records for an idx, if any.

    File: data/auto_parse/level_freeze/attempts/idx_<N>.json
    Schema: list of {attempt: int, ts: str, outcome: str, diagnostic: str,
                     parser_diff_summary: str, monkey_patch_hits: list[str]}
    """
    path = ATTEMPTS_DIR / f"idx_{idx}.json"
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return []


def render_attempt_history(idx: int) -> tuple[int, str]:
    """Return (next_attempt_number, history_block_for_prompt).

    Returns (1, "") if no prior attempts. Otherwise returns the count
    of prior attempts and a formatted ALL-CAPS banner showing what each
    failed attempt tried, so the next attempt does not repeat.
    """
    attempts = load_attempts(idx)
    if not attempts:
        return 1, ""

    next_n = len(attempts) + 1
    parts = [
        f"## ⚠ ATTEMPT {next_n} OF 3 FOR idx={idx} — PRIOR ATTEMPTS FAILED ⚠",
        "",
        "Earlier attempts on this idx are summarized below. **DO NOT REPEAT**",
        "what they tried — every approach listed here was already rejected,",
        "and trying it again wastes a dispatch.",
        "",
    ]
    for a in attempts:
        parts.append(
            f"### ATTEMPT {a.get('attempt', '?')} ({a.get('ts', '?')}) — {a.get('outcome', 'failed')}"
        )
        diag = a.get("diagnostic") or ""
        if diag:
            parts.append("Validator diagnostic:")
            for line in diag.splitlines()[:12]:
                parts.append(f"    {line}")
            extra = len(diag.splitlines()) - 12
            if extra > 0:
                parts.append(f"    … (+{extra} more diagnostic lines)")
        mp = a.get("monkey_patch_hits") or []
        if mp:
            parts.append("Monkey-patch hits in parser source (FORBIDDEN):")
            for h in mp[:5]:
                parts.append(f"    {h}")
            if len(mp) > 5:
                parts.append(f"    … (+{len(mp) - 5} more hits)")
        diff = a.get("parser_diff_summary") or ""
        if diff:
            parts.append(f"Parser diff: {diff}")
        parts.append("")

    parts.append("If your plan resembles any of the above, STOP and pick a")
    parts.append("DIFFERENT approach. Read task_rules/scope_rule.md again.")
    parts.append("")
    return next_n, "\n".join(parts)


PROMPT_TEMPLATE = """\
# clause-extract parser-tuning loop — dispatch for idx={current_idx}

The parser's only goal is to slice each agreement's HTML into clauses
with hierarchy — clause text plus **nesting depth** — so that
concatenating the spans in document order reconstructs the source
faithfully. Your job: tune `parse_doc2dict_with_config.py` and/or
`src/clause_extract/agreement_config.py` so the parser produces
rubric-compliant, reconstruction-faithful output for idx={current_idx}
without regressing already-frozen idxs.

Each dispatch handles **EXACTLY ONE idx**. After one successful
freeze + advance for idx={current_idx}, **STOP and EXIT** — the driver
will spawn a fresh dispatch for the next idx. Do NOT continue editing,
running parser, freezing, or advancing for any later idx in this
session. ONE ADVANCE PER DISPATCH.

The driver gives you up to 3 dispatches per idx if a dispatch fails
(no advance). Failure history is shown below if any.
{attempt_history}
## Source of truth (canonical)

The rubric, the scope rule, and the worked examples are the single
source of truth for what a "correct" parse looks like:

  - `task_rules/level_rubric.md` — depth 0–7 rubric (read `level`
    as nesting depth), SEC-envelope drop, subdocument depth penalty,
    reconstruction-faithfulness bar.
  - `task_rules/scope_rule.md` — STRUCTURAL agreement-vs-trailer
    classification using signature-block + real-subdoc detection.
    Document type is irrelevant (private, government, unilateral,
    multilateral all use the same rules).
  - `task_rules/examples_main_agreement.md` — flat agreement worked.
  - `task_rules/examples_with_subdocs.md` — subdoc penalty up to L7.
  - `task_rules/freeze_command.md` — what the freeze validator checks
    (rubric, monkey-patch, AND reconstruction gates are all BLOCKING:
    word coverage < 90% → freeze refused).

READ AT LEAST `level_rubric.md` AND `scope_rule.md` BEFORE EDITING.

The corpus's source-of-truth dump is at
`data/auto_parse/parse_source_of_truth.jsonl` — bs4 plain text per
idx in `span_clean`, raw HTML in `span_html`. Concat-of-spans from
your parser should approximate `span_clean[idx]` for idx={current_idx}.

## Scope: every kind of agreement is in scope

All agreement types parse under the same rubric: private bilateral
contracts, **government contracts and amendments** (SF-30 etc.),
international/cross-border agreements, **unilateral instruments**
(guaranties, options, releases, designations of agent, irrevocable
proxies), multilateral / tri-party agreements. The structural
patterns the parser detects (numbered Sections, lettered subsections,
signature lines, attached subdocuments) are common to all of them.

Out of scope is **non-agreement metadata only** — the SEC envelope,
post-signature filing trailers (press releases, About-Company,
forward-looking-statement disclaimers, contact blocks). The scope rule
in `task_rules/scope_rule.md` filters those structurally; it does NOT
filter by what kind of agreement the document is. Do NOT add
document-class-specific code (`if is_government_contract`, `if
is_unilateral`) — if a branch by document class feels needed, the
structural rule needs fixing instead.

## Level rubric (one-screen summary — full version in level_rubric.md)

Each JSONL record has four keys: `idx`, `order`, `level`, `span`.

  - `order` is a 0-indexed sequence number within idx, in document
    order. First emitted span has `order=0`, second has `order=1`,
    etc. Monotonic per-idx, no gaps.
  - `level` is a 0-indexed **nesting depth** into the agreement's
    structural hierarchy.

Standalone agreement (no subdocuments):

  - Depth 0 = the agreement **TITLE ALONE** (exactly one record per
    idx). The preamble is NOT depth 0 — it is depth 1.
  - Depth 1 = every direct child of the agreement: preamble
    paragraph, recitals block, **each top-level body clause**
    (Article when the doc uses Article/Section nesting, otherwise
    the numbered Section), and the signature block.
  - Depth 2 = direct children of L1 clauses: Section under Article,
    or lettered "(a)/(b)/(c)" subsection under a top Section.
  - Depth 3 = direct children of L2 clauses: lettered "(a)" under
    Section-under-Article, or roman/letter/digit "(i)/(A)/(1)"
    items under "(a)".
  - Depth 4+ = deeper nesting (rare in EX-10 main bodies).
  - Subdocument penalty: each subdoc ancestor (cls=exhibit/schedule/
    appendix/annex with descriptive title, NOT the SEC envelope)
    adds +1 to every descendant's depth. Ceiling 7.

The agreement title and the preamble paragraph are TWO records:
title → L0 alone; preamble → L1 (one of the agreement's children).

For documents with a flatter structure (a unilateral designation, a
one-page release, a government form with numbered fields), the
deeper depths may simply not appear — that is correct. Depths
capture structure that exists in the source; don't synthesise depth
that isn't there.

The SEC envelope ("EXHIBIT 10.25" with empty body, FIRST exhibit-class
section per idx) is `is_envelope=true`, kept in parquet, dropped from
JSONL — already implemented; do not touch.

## Scope rule (one-screen summary — full version in scope_rule.md)

A section is OUT OF SCOPE for JSONL emission if BOTH:

  1. It appears AFTER the main agreement's SIGNATURE BLOCK, AND
  2. It is NOT a descendant of a REAL SUBDOCUMENT.

Detection (STRUCTURAL only — NEVER phrase-matched):

  - SIGNATURE BLOCK = the cluster ending at the LAST section whose
    direct text contains a `/s/` + whitespace + word-chars signature
    line, extending through following same-depth `By:` / `Name:` /
    `Title:` / `Address:` / second-signatory records.
  - REAL SUBDOCUMENT = section with cls in
    "exhibit"/"schedule"/"appendix"/"annex", `is_envelope=false`, AND
    title with descriptive text after the bare identifier (an em-dash,
    en-dash, hyphen, or colon followed by ≥ 1 descriptive word).

Out-of-scope sections get tagged `scope="trailer"` in the parquet.
In-scope sections get `scope="agreement"`. ONLY `scope="agreement"`
records emit to JSONL.

A press release attached as `EXHIBIT B — FORM OF JOINT PRESS RELEASE`
is a REAL subdoc → stays. A bare post-signature press-release block
with no exhibit/schedule header is a TRAILER → dropped.

## Current state

  current_idx = {current_idx}
  frozen      = {frozen_list}

### Source-of-truth excerpt for idx={current_idx}

```
{sot_excerpt}
```

### Current parser output for idx={current_idx}

```
{current_output}
```

### Frozen baselines (must not regress)

{frozen_summary}

## What is FORBIDDEN — automatic freeze rejection

`freeze.py` scans your parser source code for these shapes BEFORE
accepting a baseline. Any hit = freeze refused, attempt wasted.

  1. **Named blocklist constants** — any module-level constant
     whose name resembles `*_BOILERPLATE`, `*_TRAILER_PATTERNS`,
     `*_BLOCKLIST`, `*_BAD_PHRASES`, `*_FILTER_PHRASES`, etc.
     Storing trailer phrases in a renamed list does not help; the
     detector is name-aware.

  2. **Trailer keywords inside ANY string literal**, including
     plain `"..."`, raw `r"..."`, triple-quoted `\"\"\"...\"\"\"`,
     f-strings, regex pattern strings inside `re.compile(...)`.
     The detector decodes every string literal in the parser source
     and flags any whose content contains a trailer KEYWORD PAIR:
       - press AND release
       - forward AND looking
       - (media|investor|press|analyst) AND contact
       - about AND (momenta|csl|company|pharmaceuticals)
       - conference AND call
       - confidential AND treatment
       - investor AND relations

     This means you CANNOT bury trailer-content matching inside a
     regex like `r"(?:MEDIA|INVESTOR|PRESS|ANALYST)\\s*CONTACT[:\\s]"`
     — both "MEDIA" and "CONTACT" appear in that string, so the pair
     check fires.

  3. **Level capping arithmetic**, e.g. `level = min(level, 7)`,
     `if level > 5: level = 5`, etc. Never clamp levels to satisfy
     the rubric ceiling — the actual content must be filtered.

These bans apply regardless of how cleverly you embed them.

## What IS ALLOWED — and what you SHOULD use

Phrase-matching IS allowed for STRUCTURAL features only:

  - Numbered/lettered/roman headers ("1.", "(a)", "(i)", "ARTICLE I").
  - Section / Subsection / Article / Schedule / Exhibit / Appendix /
    Annex headers — these are STRUCTURAL keywords and EXPECTED in
    the parser's level patterns.
  - Signature-line detection (`/s/...`).
  - Party-block patterns (`By:`, `Name:`, `Title:`, `Address:`).

Phrase-matching is NOT allowed for content classification:

  - Trailer / press-release / boilerplate / contact-block detection.
  - "Find this content and drop it" rules.

For trailer filtering, use the STRUCTURAL scope rule above. The
parser already has all the metadata you need:
  - Each section has `cls` (introduction / exhibit / schedule /
    appendix / annex / predicted header / promoted text leaf).
  - Each section has `is_envelope` (already set; the SEC envelope
    is the first exhibit-class section per idx).
  - You can compute SIGNATURE-BLOCK position by walking the section
    list and finding the last section whose body text matches the
    signature line pattern.
  - You can compute REAL-SUBDOC ANCESTRY by tracking which sections
    are descendants of a section whose `cls` is exhibit/schedule/
    appendix/annex AND whose `title` has descriptive text.

Once you have those, set `scope="agreement"` or `scope="trailer"`
on each section in `walk_sections`, and skip `scope=="trailer"`
sections in the JSONL writer. The parquet keeps every section.

## Edit guidance for THIS dispatch

The loop is INCREMENTAL — keep changes targeted:

- For idx=0 (already frozen) you must not regress. Run regress.py
  AFTER your edit, BEFORE freeze, to catch breakage early.
- Anchor STRUCTURAL regex patterns (`^...$`) so they fire only on
  legitimate header positions. This is for level/title detection
  ONLY — never for trailer filtering.
- Edit ONE thing at a time. If you add the scope rule and it breaks
  idx=0, your scope-rule code is too aggressive — narrow it.
- The current parser output above is what you start from. Read the
  level distribution and the offending records FIRST, then decide.

## Critical workflow rule: re-run the parser after EVERY edit

`regress.py` and `freeze.py` both read the canonical jsonl on disk
at `{data_dir_abs}/parse_doc2dict_with_config_nodes.jsonl`. That file
is **only refreshed when you run the parser**. If you edit the parser
source and skip the parser run, regress.py will compare frozen
baselines against the OLD jsonl from the previous parser run — giving
phantom regressions that have nothing to do with your latest edit.

**The single most common dispatch failure is reading stale jsonl after
multiple edits.** It looks like this:

```
1. Edit parser (attempt 1)
2. Run parser   ← jsonl refreshed
3. Run regress  ← REGRESSION (real)
4. Edit parser (attempt 2)
5. Run regress  ← REGRESSION (FALSE — still reading attempt-1 jsonl)
6. Edit parser (attempt 3)
7. Run regress  ← REGRESSION (FALSE — still reading attempt-1 jsonl)
   ... agent gets stuck thinking the rule is fundamentally wrong
```

**Rule: every edit to `parse_doc2dict_with_config.py` or
`agreement_config.py` MUST be followed by a parser run BEFORE any
inspection of the jsonl, regress.py invocation, or freeze attempt.**
The mtime check in step 4 below is the canonical guard — if the jsonl
mtime is older than your most recent parser-source mtime, the jsonl
is stale and any regress signal you read is a phantom.

## Workflow for THIS dispatch

1. Read the source-of-truth and the current output above. Decide which
   minimal edit to `scripts/parse_doc2dict_with_config.py`
   or `src/clause_extract/agreement_config.py` makes the
   levels for idx={current_idx} match the rubric.

2. Make the edit. Keep it minimal — adding/tightening one regex,
   reshaping the per-level pattern list. Do NOT refactor unrelated code.

3. Re-run the parser on idx={current_idx}. Run from REPO_ROOT
   ({repo_dir_abs}) and pass the ABSOLUTE `--output-dir` so the canonical
   jsonl gets updated. `freeze.py` and `regress.py` read from
   `{data_dir_abs}/parse_doc2dict_with_config_nodes.jsonl`; if the parser
   writes elsewhere, the freeze will silently capture stale output.

     cd {repo_dir_abs} && \\
       uv run scripts/parse_doc2dict_with_config.py \\
         --limit {limit_n} \\
         --output-dir {data_dir_abs} \\
         --no-truncate

   `--limit {limit_n}` parses idxs 0..{current_idx} inclusive. `--no-truncate`
   keeps full spans so the freeze captures real text.

4. **Sanity check the parser ran AND emitted records for idx={current_idx}.**
   Two failure modes to catch here:
   (a) the parser run was skipped or wrote to the wrong directory, so
       the canonical jsonl is stale relative to your parser source;
   (b) the parser ran but your edit silently dropped the target idx
       from the output, so the freeze would say "no records for
       idx={current_idx}" and burn an attempt.

   Verify both:

     # The canonical jsonl must be NEWER than the parser source —
     # otherwise it reflects a previous parser version and any regress
     # signal will be a phantom.
     stat -f "%m %N" {data_dir_abs}/parse_doc2dict_with_config_nodes.jsonl scripts/parse_doc2dict_with_config.py src/clause_extract/agreement_config.py | sort
     # The jsonl line should be LAST (largest mtime). If a source file
     # has a newer mtime, you must re-run the parser before continuing.

     grep -c '"idx": {current_idx}' {data_dir_abs}/parse_doc2dict_with_config_nodes.jsonl
     # count must be > 0; if 0, your edit broke parsing for this idx —
     # revert and try a smaller change.

5. **Run the regression check** before freezing:

     uv run scripts/level_loop/regress.py

   If this fails, your edit broke a previously frozen idx. Loop back to
   step 2 with a SMALLER, MORE TARGETED change. Do not proceed to
   freeze with a regressed parser — that just wastes attempts.

6. Inspect the output for idx={current_idx} in
   `{data_dir_abs}/parse_doc2dict_with_config_nodes.jsonl`. Verify two
   things:

   (a) **Rubric**: exactly one depth-0 record, max depth ≤ 7, no
       envelope as first record, no SEC-filing boilerplate captured.

   (b) **Reconstruction**: concat-of-spans must approximate the
       source-of-truth. Word coverage < 90% is a HARD FAIL —
       the freeze gate enforces this automatically. To preview
       the check before freezing, run:

         uv run scripts/level_loop/regress.py

       If regress passes, word coverage is sufficient. If you
       suspect content was dropped, inspect the output jsonl:

         python3 -c "
import json, re
rows=[json.loads(l) for l in open('{data_dir_abs}/parse_doc2dict_with_config_nodes.jsonl') if '\"idx\": {current_idx}' in l]
spans=' '.join(r.get('body_direct','') for r in rows)
print(len(spans.split()), 'words in reconstruction')
"

       Find missing words in the source, locate the record(s)
       that should have captured them, and fix the slicing.
       Then re-run the parser and try again.

7. If correct, freeze:

     uv run scripts/level_loop/freeze.py {current_idx}

   freeze.py validates the records AND scans the parser source. It
   refuses on ANY of:
     - rubric violations (0 or >1 depth-0 records, max depth > 7,
       first record = SEC envelope marker, filing-trailer pattern
       found in any span);
     - monkey-patch hits in parser source (named blocklist constants,
       trailer keyword pairs in any string literal, level-capping
       arithmetic).

   freeze.py also enforces a BLOCKING reconstruction gate: if word
   coverage for this idx is below 90%, the freeze is refused. The
   error message includes the percentage, char ratio, count of
   missing words, and a sample of the missing words. Use that sample
   to localize what got dropped, fix the parser slicing, re-run the
   parser, and try freeze again.

   If freeze rejects for ANY reason (rubric, monkey-patch, or
   reconstruction), READ THE DIAGNOSTIC, fix the parser source
   accordingly (do NOT add new monkey-patches; remove the offending
   shape; for reconstruction, find the missing content), re-run the
   parser, and try freeze again WITHIN this dispatch. Do NOT pass
   --force.

8. Advance to the next idx — EXACTLY ONCE:

     uv run scripts/level_loop/advance.py

   `advance.py` runs regress.py as a gate; it refuses if any frozen
   idx fails. Do not call advance with --skip-regress.

9. **STOP IMMEDIATELY AFTER ONE SUCCESSFUL ADVANCE.** Print a single
   one-line summary of what changed for idx={current_idx} and EXIT.
   DO NOT continue with idx={current_idx}+1. The driver dispatches a
   fresh opencode session for each idx; your job is exactly ONE idx.

## Hard rules

- Edit only `parse_doc2dict_with_config.py` and/or
  `src/clause_extract/agreement_config.py`.
- Never edit files under `data/auto_parse/level_freeze/frozen/` by
  hand.
- Never call `freeze.py --force`. Only humans force.
- **DO NOT ASK QUESTIONS.** This is a fully autonomous dispatch — there
  is no human to answer. When you face a judgment call, make the most
  pragmatic choice that lets the rubric pass and the freeze advance,
  and document your reasoning in the commit message and/or attempt
  notes. Asking and exiting wastes a dispatch slot.
- **doc2dict text-loss fallback.** If doc2dict has dropped content
  the rubric expects (title/preamble missing, content past page-break
  HRs gone, etc.), accept what is available: promote the first
  available section as L0 and freeze. Note the truncation in your
  commit message. Do NOT block on this — it is a known limitation
  and downstream tooling will surface incomplete spans.
- If you cannot make the rubric pass without breaking a frozen idx,
  STOP, print one line of WHY, and exit. The driver will retry with
  a fresh dispatch (with your failure recorded in attempt history).
- DO NOT use phrase blocklists, trailer-keyword regexes, or level
  capping. Use the structural scope rule.
- DO NOT add document-class-specific code paths
  (`if is_government_contract`, `if is_unilateral`, etc.). The
  structural rules apply uniformly across agreement types; branches
  by document class mean the rule itself needs rethinking. The parser
  works the same for private, government, unilateral, and
  international agreements.
- **ONE ADVANCE PER DISPATCH.** After `advance.py` succeeds for
  idx={current_idx}, EXIT. Do NOT loop into another idx. The driver
  is responsible for the next idx; the agent's scope is exactly one.
- `freeze.py` may be retried within the same dispatch after fixing a
  rejection (re-run the parser first, then re-call freeze). Do NOT
  pass `--force`. Never call `advance.py` twice. If you find yourself
  looking at idx={current_idx}+1 after a successful advance, you are
  DONE — stop now.
"""


def main() -> None:
    state = load_state()
    current_idx = int(state["current_idx"])
    frozen = list(state.get("frozen", []))

    sot = excerpt_sot(current_idx)
    current_output = render_current_output(current_idx)
    frozen_summary = render_frozen_summary(frozen)
    _, attempt_history = render_attempt_history(current_idx)

    prompt = PROMPT_TEMPLATE.format(
        current_idx=current_idx,
        frozen_list=frozen,
        sot_excerpt=sot,
        current_output=current_output,
        frozen_summary=frozen_summary,
        span_preview=_SPAN_PREVIEW,
        limit_n=current_idx + 1,
        data_dir_abs=str(DATA),
        repo_dir_abs=str(REPO),
        attempt_history=attempt_history,
    )
    sys.stdout.write(prompt)


if __name__ == "__main__":
    typer.run(main)

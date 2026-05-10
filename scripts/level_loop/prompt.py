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
# clause-extract level-tuning loop — dispatch for idx={current_idx}

Your job: tune `parse_doc2dict_with_config.py` and/or
`src/clause_extract/agreement_config.py` so the parser produces
RUBRIC-COMPLIANT output for idx={current_idx} without regressing
already-frozen idxs.

Each dispatch handles **EXACTLY ONE idx**. After one successful
freeze + advance for idx={current_idx}, **STOP and EXIT** — the driver
will spawn a fresh dispatch for the next idx. Do NOT continue editing,
running parser, freezing, or advancing for any later idx in this
session. ONE ADVANCE PER DISPATCH.

The driver gives you up to 3 dispatches per idx if a dispatch fails
(no advance). Failure history is shown below if any.
{attempt_history}
## Source of truth (canonical)

The level rubric, the scope rule, and the worked examples are the
single source of truth for what a "correct" parse looks like:

  - `task_rules/level_rubric.md` — levels 0–7, SEC-envelope drop,
    subdocument level penalty.
  - `task_rules/scope_rule.md` — STRUCTURAL agreement-vs-trailer
    classification using signature-block + real-subdoc detection.
  - `task_rules/examples_main_agreement.md` — flat agreement worked.
  - `task_rules/examples_with_subdocs.md` — subdoc penalty up to L7.
  - `task_rules/freeze_command.md` — what the freeze validator checks.

READ AT LEAST `level_rubric.md` AND `scope_rule.md` BEFORE EDITING.

The corpus's source-of-truth dump is at
`data/auto_parse/parse_source_of_truth.jsonl` — bs4 plain text +
raw HTML per idx. Use it to verify your parse's reconstruction.

## Level rubric (one-screen summary — full version in level_rubric.md)

Levels are 0-indexed depths in the agreement's structural hierarchy:

  - L0 = the agreement itself (EXACTLY one record per idx: title +
    preamble paragraph).
  - L1 = top-level headings + recitals: party metadata, WITNESSETH /
    WHEREAS, signature block, real-subdoc HEADERS.
  - L2 = numbered Sections ("1.", "ARTICLE 1", "Section 1.1").
  - L3 = lettered subsections ("(a)", "(b)").
  - L4 = sub-sub items ("(i)", "(A)", "(1)").
  - L5–L7 = same as L2–L4 but inside 1, 2, or 3 levels of subdoc
    nesting (each subdoc adds +1 penalty to descendants).

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
   `{data_dir_abs}/parse_doc2dict_with_config_nodes.jsonl`. Verify the
   levels match the rubric: exactly one level-0, no levels > 7 unless
   the document has 3+ subdocs, no SEC-filing boilerplate captured.

7. If correct, freeze:

     uv run scripts/level_loop/freeze.py {current_idx}

   freeze.py validates the records AND scans the parser source. It
   refuses on ANY of:
     - rubric violations (0 or >1 level-0 records, max level > 7,
       first record = SEC envelope marker, filing-trailer pattern
       found in any span);
     - monkey-patch hits in parser source (named blocklist constants,
       trailer keyword pairs in any string literal, level-capping
       arithmetic).

   If freeze rejects, READ THE DIAGNOSTIC, fix the parser source
   accordingly (do NOT add new monkey-patches; remove the offending
   shape), re-run the parser, and try freeze again WITHIN this
   dispatch. Do NOT pass --force.

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
- If you cannot make the rubric pass without breaking a frozen idx,
  STOP, print one line of WHY, and exit. The driver will retry with
  a fresh dispatch (with your failure recorded in attempt history).
- DO NOT use phrase blocklists, trailer-keyword regexes, or level
  capping. Use the structural scope rule.
- **ONE ADVANCE PER DISPATCH.** After `advance.py` succeeds for
  idx={current_idx}, EXIT. Do NOT loop into another idx. The driver
  is responsible for the next idx; the agent's scope is exactly one.
- **NEVER call freeze.py or advance.py twice in one dispatch.** If
  you somehow find yourself looking at idx={current_idx}+1 after a
  successful advance, that means you should be DONE — stop now.
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

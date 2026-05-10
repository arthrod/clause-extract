# task_rules — level-tuning loop reference

This folder contains the rules and prompts used by the `scripts/level_loop/`
self-correcting loop. Each turn, an opencode agent reads one of these
prompts (composed by `prompt.py`) and tunes the parser at
`scripts/parse_doc2dict_with_config.py` until one source agreement parses
with rubric-compliant levels, freezes the result, then advances to the
next idx.

These files are the source of truth for what "correct" means. If the
rubric changes, edit `level_rubric.md` first, then propagate through
`turn_prompt.md` and re-bake any frozen baselines that no longer match.

## Files

- [`level_rubric.md`](level_rubric.md) — the full level 0–7 rubric, with
  the SEC-envelope drop rule and the subdocument level-penalty rule.
- [`scope_rule.md`](scope_rule.md) — the structural in-scope/out-of-
  scope rule for filtering filing-trailer content (press releases,
  About-Company, Forward-Looking Statements, etc.) using
  signature-block + real-subdoc detection. NEVER use phrase blocklists.
- [`turn_prompt.md`](turn_prompt.md) — the per-turn ACP prompt template.
  This is what `scripts/level_loop/prompt.py` emits, with a few `{vars}`
  filled in at run time. Reading this file shows you exactly what an
  agent sees on every turn.
- [`freeze_command.md`](freeze_command.md) — when and how to freeze the
  current idx. Full copy-paste command.
- [`regress_command.md`](regress_command.md) — regression check across
  all frozen idxs. Full copy-paste command.
- [`advance_command.md`](advance_command.md) — bump `current_idx` to the
  next sample. Refuses to advance if regression fails.
- [`examples_main_agreement.md`](examples_main_agreement.md) — worked
  example: an Indemnification Agreement parsed to levels 0–4. Shows the
  expected (level, title) pairs.
- [`examples_with_subdocs.md`](examples_with_subdocs.md) — worked
  example: an agreement with attached "EXHIBIT A — FORM OF NOTICE"
  subdocument. Shows how the +1 penalty stacks down to level 7.

## Quick reference: complete loop, one turn

```bash
# From: /Users/arthrod/temp/T/clause-extract/clause-extract
MODEL=zai-coding-plan/glm-5.1 ./scripts/level_loop/turn.sh
```

That script regenerates the prompt from `prompt.py`, snapshots it under
`data/auto_parse/level_freeze/turns/`, and dispatches one fresh-context
opencode session. The agent edits, re-runs the parser, freezes,
regresses, and advances. When `state.json["current_idx"]` increments,
the turn succeeded.

# task_rules — parser-tuning loop reference

This folder contains the rules and prompts used by the
`scripts/level_loop/` self-correcting loop. The loop's goal is to tune
`scripts/parse_doc2dict_with_config.py` so it slices each EX-10
agreement's HTML into clauses with hierarchy — clause text plus nesting
depth — such that concatenating the spans in document order reconstructs
the source.

Each turn, an opencode agent reads the prompt composed by
`scripts/level_loop/prompt.py`, tunes the parser, freezes the result for
one idx, regresses against previously frozen idxs, then advances.

These md files are the source of truth for what "correct" means. If the
rubric changes, edit `level_rubric.md` first, then propagate through the
other files; if existing frozen baselines no longer match, re-bake them.

## Scope reminder

The parser handles **all agreement types** — private, public,
government (contract amendments like SF-30), international, unilateral
(guaranties, releases, designations), multilateral. Document type does
not change the rules. The only out-of-scope content is **non-agreement
metadata** that travels with the SEC filing (the envelope, post-
signature filing trailers). See [`scope_rule.md`](scope_rule.md).

## Files

- [`level_rubric.md`](level_rubric.md) — depth 0–7 rubric (read
  `level` as nesting depth), SEC-envelope drop, subdocument depth
  penalty, and the reconstruction-faithfulness bar.
- [`scope_rule.md`](scope_rule.md) — structural in-scope/out-of-scope
  rule for filtering non-agreement metadata using signature-block +
  real-subdoc detection. NEVER use phrase blocklists.
- [`turn_prompt.md`](turn_prompt.md) — per-turn ACP prompt template.
  This is what `scripts/level_loop/prompt.py` emits, with a few
  `{vars}` filled in at run time.
- [`freeze_command.md`](freeze_command.md) — freezing one idx as a
  golden baseline. Includes the rubric/monkey-patch gates (blocking)
  and the new reconstruction warning (non-blocking).
- [`regress_command.md`](regress_command.md) — regression check
  across all frozen idxs.
- [`advance_command.md`](advance_command.md) — bump `current_idx` to
  the next sample. Refuses to advance if regression fails.
- [`examples_main_agreement.md`](examples_main_agreement.md) — worked
  example: an Indemnification Agreement parsed to depths 0–4.
- [`examples_with_subdocs.md`](examples_with_subdocs.md) — worked
  example: an agreement with attached "EXHIBIT A — FORM OF NOTICE"
  subdocument, showing how the +1 penalty stacks down to depth 7.

## Quick reference: complete loop, one turn

```bash
# From: /Users/arthrod/temp/T/clause-extract
MODEL=zai-coding-plan/glm-5.1 ./scripts/level_loop/turn.sh
```

That script regenerates the prompt from `prompt.py`, snapshots it under
`data/auto_parse/level_freeze/turns/`, and dispatches one fresh-context
opencode session. The agent edits, re-runs the parser, runs the
reconstruction check, freezes, regresses, and advances. When
`state.json["current_idx"]` increments, the turn succeeded.

## Run N turns

```bash
MODEL=zai-coding-plan/glm-5.1 ./scripts/level_loop/run_n_turns.sh 7
```

Processes N idxs sequentially. Each idx gets up to 3 retries; if all
three fail, the driver records the failure and force-advances. See
`scripts/level_loop/run_n_turns.sh` for the policy.

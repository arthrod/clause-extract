# task_rules — parser-tuning loop reference

This folder contains the rules and prompts used by the
`scripts/level_loop/` self-correcting loop. The loop's goal is to tune
`scripts/parse_doc2dict_with_config.py` so it slices each EX-10
agreement's HTML into clauses with hierarchy — clause text plus nesting
depth — such that concatenating the spans in document order
reconstructs the source.

Each turn, an agent reads the prompt composed by
`scripts/level_loop/prompt.py`, tunes the parser, freezes the result
for one idx, regresses against previously frozen idxs, then advances.

These md files are the source of truth for what "correct" means. If
the rubric changes, edit `level_rubric.md` first, then propagate
through the other files; if existing frozen baselines no longer match,
re-bake them.

## Rubric in one paragraph

The agreement title alone is depth 0. Every direct child of the
agreement — preamble paragraph, recital block, each top-level body
clause (Article when the doc nests Sections inside Articles; otherwise
the numbered Section), and the signature block — is depth 1. Direct
children of those are depth 2; their children depth 3; and so on. A
real subdocument (`cls in {exhibit, schedule, appendix, annex}`,
`is_envelope=False`, descriptive title) adds +1 to every descendant's
depth. Ceiling is depth 7. See [`level_rubric.md`](level_rubric.md).

## Scope reminder

The parser handles **all agreement types** — private, public,
government (contract amendments like SF-30), international, unilateral
(guaranties, releases, designations), multilateral. Document type does
not change the rules. The only out-of-scope content is **non-agreement
metadata** that travels with the SEC filing (the envelope, post-
signature filing trailers). See [`scope_rule.md`](scope_rule.md).

## Files

Rubric / contract:

- [`level_rubric.md`](level_rubric.md) — the depth 0–7 contract.
  Defines `idx`/`order`/`level`/`span`/`cls`/`is_envelope`/`scope`
  in the Terminology section, gives the standalone-agreement depth
  table, the SEC-envelope drop, the +1 subdocument penalty, common
  parser failure modes, and the 90% reconstruction gate.
- [`scope_rule.md`](scope_rule.md) — the structural rule for what
  is "the agreement" vs filing context. Covers pre-title cover
  wedges, the trailer-types table, signature-block detection,
  real-subdocument detection, the bare-identifier subdoc rescue,
  and an end-to-end worked walkthrough.

Worked examples:

- [`examples_main_agreement.md`](examples_main_agreement.md) — the
  Indemnification Agreement baseline (no subdocs; depths 0–3).
- [`examples_with_subdocs.md`](examples_with_subdocs.md) — a
  synthetic agreement with three levels of nested subdocs to show
  the +1 penalty stacking through depth 7.

Loop commands (run in order each turn):

- [`freeze_command.md`](freeze_command.md) — freeze one idx as a
  golden baseline. Documents the rubric, monkey-patch, and
  reconstruction gates (all blocking).
- [`regress_command.md`](regress_command.md) — regression check
  across every frozen idx. Run after each parser edit; advance
  re-runs it as a gate.
- [`advance_command.md`](advance_command.md) — bump `current_idx`
  to the next sample. Refuses to advance if regression fails.

Agent prompt:

- [`turn_prompt.md`](turn_prompt.md) — per-turn ACP prompt template
  emitted by `scripts/level_loop/prompt.py` with `{vars}` filled in
  at run time. This is what the agent sees on each turn.

## JSONL record shape

```json
{"idx": <int>, "order": <int>, "level": <int>, "span": "<heading>\n<body>"}
```

  - `order` is 0-indexed per-idx, document order. JSON dicts aren't
    ordered; `order` is the explicit sequence number so downstream
    consumers reconstruct the linear order without relying on key
    ordering. See `level_rubric.md` for details.

## Quick reference: complete loop, one turn

```bash
cd /Users/arthrod/temp/T/clause-extract
./scripts/level_loop/turn.sh
```

That script regenerates the prompt from `prompt.py`, snapshots it
under `data/auto_parse/level_freeze/turns/`, and dispatches one
fresh-context agent session. The agent edits the parser if needed,
re-runs it, runs the reconstruction check, freezes, regresses, and
advances. When `state.json["current_idx"]` increments, the turn
succeeded.

## Run N turns

```bash
./scripts/level_loop/run_n_turns.sh 7
```

Processes N idxs sequentially. Each idx gets up to 3 retries; if all
three fail, the driver records the failure and force-advances. See
`scripts/level_loop/run_n_turns.sh` for the policy.

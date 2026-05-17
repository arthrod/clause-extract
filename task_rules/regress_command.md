# regress — check current parser output against every frozen baseline

## When to run

After every parser edit. Before calling `advance.py`. The advance
script re-runs `regress.py` itself as a gate, but you should run it
manually first to see specific diffs if something fails — the
advance-time run only tells you that something regressed, not what.

## Command (copy-paste from repo root)

```bash
cd /Users/arthrod/temp/T/clause-extract
uv run scripts/level_loop/regress.py             # check every frozen idx
# OR
uv run scripts/level_loop/regress.py --idx 0     # check just one
```

## What it does

For each idx in `state.json["frozen"]`:

1. Reads the frozen baseline at
   `data/auto_parse/level_freeze/frozen/idx_<N>.jsonl`.
2. Reads the current parser output at
   `data/auto_parse/parse_doc2dict_with_config_nodes.jsonl`,
   filtered to the same idx.
3. Compares record-by-record on `(level, span)`.
4. On match: prints `idx=<N>: OK (<count> records)`.
5. On mismatch: prints a unified diff and exits with code 1.

`order` is not compared directly — it's redundant given the
record-by-record positional comparison. `idx` is implicit in the
filter. So the equivalence check is exactly `(level, span)` in line
order.

## Reading a regression diff

The diff format is `level=<n> | <span first chars>`. Example:

```
- level=2 | 1. Indemnity of Indemnitee \n The Company hereby agrees…
+ level=3 | 1. Indemnity of Indemnitee \n The Company hereby agrees…
```

`-` is the frozen baseline, `+` is current. Here, the agent's edit
demoted "1. Indemnity" from level 2 to level 3 — that's a regression.

If you see record-count drift instead of a depth flip, the diff will
have unbalanced `-`/`+` blocks: one side has more records than the
other. That means the parser is splitting or merging differently
now — the regex driving the slicing changed shape.

## Common outcomes

- **All-OK.** Safe to advance. Every frozen idx reproduces under the
  current parser.
- **One-line diff (level changed).** Edit was too aggressive on
  depth assignment. Look for the rule you tightened — it now
  matches something it shouldn't, or stopped matching something it
  should.
- **One-line diff (span changed).** Edit changed text capture
  (whitespace, heading concatenation, body boundaries). Usually a
  slicing-boundary regex moved.
- **Many-line diff (record count changed).** Structural change. The
  walk produced a different number of records — either splitting or
  merging differently. Probably a regex too greedy or too narrow.
- **All-OK except one idx.** Localized regression. Read the diff for
  that idx, decide whether the new behaviour is correct (in which
  case re-bake the baseline with `freeze.py --force`) or wrong (in
  which case fix the parser).

## Common failure modes

- **"frozen baseline for idx M not found"** — `state.json` lists an
  idx that doesn't have a file under `frozen/`. Either the file was
  deleted or `state.json` was hand-edited. Re-freeze or remove from
  the list.
- **"current parser output missing idx M"** — the parser was run
  with a `--limit` smaller than M. Re-run with `--limit
  {current_idx+1}` at minimum so all frozen idxs are covered.
- **All diffs at once after a config edit.** A change to
  `agreement_config.py` shifted every document. Either the config
  edit was wrong, or the prior baselines were tuned against a bug
  that the new config fixes — decide deliberately, don't blindly
  re-bake.

## Escape hatch

There isn't one. `regress.py` is a pure check; if it fails, the
right response is to fix the parser or re-bake the offending
baseline. Bypassing regress happens at the advance step
(`advance.py --skip-regress`), and only for debugging the regress
script itself.

## See also

- [`freeze_command.md`](freeze_command.md) — how baselines get
  written.
- [`advance_command.md`](advance_command.md) — the step that runs
  regress as a gate before incrementing `current_idx`.
- [`level_rubric.md`](level_rubric.md) — the rubric whose
  satisfaction the baselines encode.

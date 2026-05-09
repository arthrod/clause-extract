# regress — check current parser output against every frozen baseline

## When to run

After every parser edit. Before calling `advance.py`. The advance
script re-runs `regress.py` itself as a gate, but you should run it
manually first to see specific diffs if something fails.

## Command (copy-paste from repo root)

```bash
cd /Users/arthrod/temp/T/clause-extract/clause-extract
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

## Reading a regression diff

The diff format is `level=<n> | <span first chars>`. Example:

```
- level=2 | 1. Indemnity of Indemnitee \n The Company hereby agrees…
+ level=3 | 1. Indemnity of Indemnitee \n The Company hereby agrees…
```

`-` is the frozen baseline, `+` is current. Here, the agent's edit
demoted "1. Indemnity" from level 2 to level 3 — that's a regression.

## Common outcomes

- All-OK: safe to advance.
- One-line diff (level changed): edit was too aggressive somewhere
  else. Look for the regex you tightened.
- Many-line diff (record count changed): structural change. The walk
  is producing a different number of records — either splitting or
  merging differently. Probably a regex too greedy or too narrow.

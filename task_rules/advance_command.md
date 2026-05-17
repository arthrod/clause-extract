# advance — bump current_idx to the next sample

## When to run

Last step of every successful turn. Only after `freeze.py` and
`regress.py` both succeeded for the current idx.

The loop driver calls `advance.py` automatically at the end of a
turn; you only invoke it directly when debugging or recovering from
a partial state.

## Command (copy-paste from repo root)

```bash
cd /Users/arthrod/temp/T/clause-extract
uv run scripts/level_loop/advance.py
```

## What it does

1. Verifies `state.current_idx` is in `state.frozen`. If not, refuses
   with a message telling you to freeze first.
2. Re-runs `regress.py` as a gate. If any frozen idx fails, refuses to
   advance.
3. Increments `state.current_idx` by 1 and appends an `advance` entry
   to `history[]` (timestamp + the new `current_idx`).

The state file is `data/auto_parse/level_freeze/state.json`. The keys
that matter are:

  - `current_idx` — the idx the next turn will tune.
  - `frozen` — list of idxs that have a baseline at
    `data/auto_parse/level_freeze/frozen/idx_<N>.jsonl`.
  - `history` — append-only log of `freeze` and `advance` events
    for audit.

## Why the gate matters

The whole point of the loop is "move forward only when everything that
worked before still works." If you advance with a regression, you've
silently corrupted a baseline; subsequent turns will be tuning against
broken expectations, and the contamination compounds turn-by-turn.

The `freeze → regress → advance` order is load-bearing:

  - **freeze** writes the new baseline for `current_idx`.
  - **regress** confirms the baseline you just wrote AND every prior
    baseline still matches the current parser output. (This catches
    the case where freezing the current idx required a parser edit
    that broke a prior idx.)
  - **advance** re-runs regress as a final gate, then increments.

## Common failure modes

- **"current_idx N is not in frozen[]"** — you tried to advance
  before freezing. Run `freeze.py` first.
- **"regression detected at idx M"** — your edit fixed
  `current_idx` but broke idx M. Roll back the breakage and find a
  smaller change that satisfies both. Don't `--skip-regress`.
- **`state.json` not found** — the loop wasn't initialised. The
  driver creates state.json on first run; if it's missing, check
  `scripts/level_loop/` for an init step.

## Escape hatch (debug only)

```bash
uv run scripts/level_loop/advance.py --skip-regress
```

This skips the regression check. Use only when explicitly debugging
the regress script itself, never in normal loop operation. The
driver does not pass `--skip-regress` in any normal path.

## See also

- [`freeze_command.md`](freeze_command.md) — the step that runs
  before advance and writes the baseline.
- [`regress_command.md`](regress_command.md) — what the gate
  actually checks.
- [`turn_prompt.md`](turn_prompt.md) — the per-turn workflow that
  ends in advance.

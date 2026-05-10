# advance — bump current_idx to the next sample

## When to run

Last step of every successful turn. Only after `freeze.py` and
`regress.py` both succeeded for the current idx.

## Command (copy-paste from repo root)

```bash
cd /Users/arthrod/temp/T/clause-extract/clause-extract
uv run scripts/level_loop/advance.py
```

## What it does

1. Verifies `state.current_idx` is in `state.frozen`. If not, refuses
   with a message telling you to freeze first.
2. Re-runs `regress.py` as a gate. If any frozen idx fails, refuses to
   advance.
3. Increments `state.current_idx` by 1 and appends an `advance` entry
   to `history[]`.

## Why the gate matters

The whole point of the loop is "move forward only when everything that
worked before still works." If you advance with a regression, you've
silently corrupted a baseline; subsequent turns will be tuning against
broken expectations.

## Escape hatch (debug only)

```bash
uv run scripts/level_loop/advance.py --skip-regress
```

This skips the regression check. Use only when explicitly debugging
the regress script itself, never in normal loop operation.

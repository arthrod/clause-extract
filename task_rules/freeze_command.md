# freeze — capture the current parser output as a regression baseline

## When to run

Only after you have re-run the parser AND visually confirmed the JSONL
output for the current idx matches the rubric. Freezing a wrong output
locks it in as the "golden" baseline and contaminates every future
regression check.

## Command (copy-paste from repo root)

```bash
cd /Users/arthrod/temp/T/clause-extract/clause-extract
uv run scripts/level_loop/freeze.py            # freezes state.current_idx
# OR explicitly:
uv run scripts/level_loop/freeze.py 0          # freezes idx=0
```

## What it does

1. Reads `data/auto_parse/parse_doc2dict_with_config_nodes.jsonl`.
2. Filters to records for the requested idx.
3. Writes them verbatim (full spans, no truncation) to
   `data/auto_parse/level_freeze/frozen/idx_<N>.jsonl`.
4. Updates `state.json`: appends idx to `frozen[]` and a `freeze` entry
   to `history[]`.

## Stale-file guard

The script refuses to freeze if the source JSONL is older than 5
minutes. This catches the case where the agent edited the parser but
forgot to re-run it (or wrote the parser output to the wrong directory).

If you genuinely need to freeze an old jsonl (e.g., re-baking after a
git checkout), pass `--force`:

```bash
uv run scripts/level_loop/freeze.py 0 --force
```

## Rubric validation gate

`freeze.py` validates the records against the rubric and **refuses to
write the baseline** if any of these are violated:

1. Exactly ONE level-0 record per idx. Zero or many ≥ 2 → reject.
2. Max level ≤ 7 (4 + 3 levels of subdoc nesting). Anything higher
   means the parser captured noise (post-signature press-release
   boilerplate, page-footer artifacts, …).
3. The first JSONL record must NOT be the SEC envelope marker
   ("EXHIBIT 10.25" et al.). The parser is supposed to drop it.
4. **Filing-trailer boilerplate detection** — if any span matches
   known SEC-filing-trailer patterns (press-release contact blocks,
   forward-looking statements, single-roman-numeral page markers,
   confidential-treatment markers, exhibit page footers), reject.
5. **Monkey-patch detector on parser source** — scans
   `scripts/parse_doc2dict_with_config.py` for forbidden code shapes:
   - hardcoded phrase blocklists (e.g. `["press release", ...]`),
   - substring checks against trailer keywords,
   - level capping (`min(level, N)`, `if level > N: level = N`),
   - company-named filtering literals.

   These are MONKEY PATCHES. The parser must use the structural scope
   rule from [`scope_rule.md`](scope_rule.md) instead.

When the gate rejects, the script prints which violation hit and
sample offending records / source lines. Fix the parser, re-run, and
try again.

`--force` bypasses the gate. Use it only when a human has explicitly
reviewed the violations and concluded the document genuinely cannot be
expressed in the rubric (extremely rare).

## Common error: refuses to overwrite

If `idx_<N>.jsonl` already exists, freeze refuses without `--force`.
That's intentional — overwriting a freeze should be deliberate. Use
`--force` only when you've explicitly decided to invalidate the
existing baseline.

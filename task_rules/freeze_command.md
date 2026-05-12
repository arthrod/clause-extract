# freeze — capture the current parser output as a regression baseline

## When to run

Only after you have re-run the parser AND confirmed the JSONL output
for the current idx is rubric-compliant. Freezing a wrong output locks
it in as the "golden" baseline and contaminates every future regression
check.

## Command (copy-paste from repo root)

```bash
cd /Users/arthrod/temp/T/clause-extract
uv run scripts/level_loop/freeze.py            # freezes state.current_idx
# OR explicitly:
uv run scripts/level_loop/freeze.py 0          # freezes idx=0
```

## What it does

1. Reads `data/auto_parse/parse_doc2dict_with_config_nodes.jsonl`.
2. Filters to records for the requested idx.
3. Runs the **rubric validation gate** and the **monkey-patch
   detector** (see below). Refuses to freeze on violations.
4. Runs the **blocking reconstruction gate** comparing concat-of-spans
   to the source-of-truth (see below). Refuses the freeze if word
   coverage is below 95% (per [`../docs/DECISIONS.md`](../docs/DECISIONS.md)
   §10).
5. Writes the records verbatim (full spans, no truncation) to
   `data/auto_parse/level_freeze/frozen/idx_<N>.jsonl`.
6. Updates `state.json`: appends idx to `frozen[]` and a `freeze` entry
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

## Rubric validation gate (blocking)

`freeze.py` validates the records against the rubric and **refuses to
write the baseline** if any of these are violated:

1. Exactly ONE depth-0 record per idx. Zero or 2+ → reject.
2. Max depth ≤ 7 (4 + 3 levels of subdoc nesting). Anything higher
   means the parser captured noise (post-signature trailer
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

## Reconstruction gate (blocking)

The parser's stated goal is that concatenating the JSONL spans for one
idx in line order reconstructs the source. `freeze.py` enforces this as
a **hard gate**: a freeze is refused if the reconstruction misses ≥ 5%
of the source's unique words.

```
word_coverage_pct = |source_words ∩ reconstructed_words| / |source_words| × 100
char_ratio_pct    = len(reconstructed) / len(source) × 100
```

where `source` is `parse_source_of_truth.jsonl[idx].span_clean` and
`reconstructed` is the concat of all `span` values in JSONL line order
for this idx. Both texts are lowercased and whitespace-collapsed before
the word-set / char-length comparison.

  - **Pass** at word coverage ≥ 95% — printed as
    `reconstruction OK: word_coverage=X.X% char_ratio=Y.Y%`.
  - **Fail** at word coverage < 95% — freeze refused. The error
    includes the percentage, char ratio, count of missing words, and
    a sample of the missing words so the agent can localize the gap.

The 95% bar comes from [`../docs/DECISIONS.md`](../docs/DECISIONS.md)
§10. Mean coverage across the corpus must clear 95% for the parser to
be considered acceptable; this freeze gate enforces the bound per-idx
so individual baselines can't sneak in below the bar.

Why blocking, not advisory: reconstruction faithfulness is the
parser's only goal (slice the HTML into clauses such that concat
reproduces the source). A freeze that fails reconstruction has
captured a structure that loses content — locking that as a golden
baseline contaminates everything downstream. Better to refuse the
freeze and force the agent to find a slicing that recovers the words.

If a particular document genuinely cannot reach 95% (e.g. the
source-of-truth bs4 extraction itself dropped content), the human
operator can pass `--force` after explicit review. That's the escape
hatch; do not use it programmatically.

## Common error: refuses to overwrite

If `idx_<N>.jsonl` already exists, freeze refuses without `--force`.
That's intentional — overwriting a freeze should be deliberate. Use
`--force` only when you've explicitly decided to invalidate the
existing baseline.

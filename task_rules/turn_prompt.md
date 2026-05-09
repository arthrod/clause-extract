# Per-turn ACP prompt template

`scripts/level_loop/prompt.py` reads `state.json`, the current parser
JSONL output, and the source-of-truth excerpt, and emits the prompt
below — one fresh prompt per turn. The loop driver pipes that prompt
into `opencode run`. Each turn has zero memory of prior turns; the
prompt is the only context the agent sees.

The fields below in `{braces}` are filled by `prompt.py`.

---

```
# clause-extract level-tuning loop — single turn

Your job in this turn is to tune `parse_doc2dict_with_config.py` (or the
mapping config it depends on) so that ONE source agreement parses with
levels that match the rubric below — without breaking any previously
frozen samples.

Each turn has fresh context. There is no memory between turns. Read the
state, make the minimal change, freeze, advance, exit.

## Level rubric (the contract)

Levels are 0-indexed depths in the parsed tree. For a typical
EX-10 agreement (e.g. an Indemnification Agreement) the expected mapping is:

  - Level 0  →  the agreement itself (single root)
  - Level 1  →  top-level headings and recital blocks (party / exhibit
                metadata, WITNESSETH/WHEREAS recitals, signature block)
  - Level 2  →  numbered Sections: "1.", "2.", "10.", "Section 1.1"
  - Level 3  →  lettered subsections: "(a)", "(b)", "(c)"
  - Level 4  →  sub-sub items: "(i)", "(ii)", "(A)", "(B)", "(1)", "(2)"

Subdocument penalty: each attached subdocument (cls=exhibit/schedule/
appendix/annex with content, NOT the SEC envelope) adds +1 to every
descendant's level. See task_rules/level_rubric.md for full details.

## Source of truth

`data/auto_parse/parse_source_of_truth.jsonl` — bs4 plain-text
extraction of each source HTML. The reconstruction (concat of parser
spans for one idx) should approximate `span_clean[idx]`. Levels graded
against the rubric.

## Current state

  current_idx = {current_idx}
  frozen      = {frozen_list}

### Source-of-truth excerpt for idx={current_idx}

```
{sot_excerpt}
```

### Current parser output for idx={current_idx}

(The `span` is truncated to {span_preview} chars in this prompt; the
full JSONL has the unbounded text.)

```
{current_output}
```

### Frozen baselines (must not regress)

{frozen_summary}

## Workflow for THIS turn

1. Read the source-of-truth and current output. Decide the minimal edit.

2. Edit `clause-extract/scripts/parse_doc2dict_with_config.py` and/or
   `clause-extract/src/clause_extract/agreement_config.py`. Keep edits
   minimal — adjust ONE pattern or rule.

3. Re-run the parser on idxs 0..{current_idx} (use ABSOLUTE paths):

     cd {repo_dir_abs} && \
       uv run scripts/parse_doc2dict_with_config.py \
         --limit {limit_n} \
         --output-dir {data_dir_abs} \
         --no-truncate

4. Verify the JSONL at
   `{data_dir_abs}/parse_doc2dict_with_config_nodes.jsonl` for
   idx={current_idx} matches the rubric. `ls -la` it first to confirm
   the mtime is recent — a stale jsonl would silently freeze wrong data.

5. Freeze the corrected output:

     uv run scripts/level_loop/freeze.py {current_idx}

6. Regression check:

     uv run scripts/level_loop/regress.py

   Failure here means your edit broke a previously frozen idx. Loop
   back to step 2 and find a smaller change.

7. Advance:

     uv run scripts/level_loop/advance.py

When advance succeeds, the turn is done. Print a one-line summary and
stop.

## Hard rules

- Edit ONLY `parse_doc2dict_with_config.py` and `agreement_config.py`.
- Never edit files under `data/auto_parse/level_freeze/frozen/` by hand.
- Never call `freeze.py --force` unless the loop driver tells you to.
- If you cannot satisfy the rubric without breaking a frozen idx, STOP,
  print why, and exit. The human running the loop will decide.
```

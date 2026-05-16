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

The parser's only goal is to slice the source HTML into clauses with
hierarchy — clause text plus nesting depth — so that concatenating
the spans in document order reconstructs the source faithfully. You
are tuning `parse_doc2dict_with_config.py` (or its mapping config)
so that ONE source agreement parses correctly under that goal, without
breaking any previously frozen samples.

Each turn has fresh context. There is no memory between turns. Read the
state, make the minimal change, freeze, advance, exit.

## What counts as "in scope"

ALL agreement types are in scope: private contracts, government
contracts and amendments (e.g. SF-30), international agreements,
unilateral instruments (guaranties, options, releases, designations),
multi-party agreements. Document type does not matter to the parser;
the structural patterns it detects are common across all of these.

Out of scope is **non-agreement metadata** only — the SEC envelope
("EXHIBIT 10.x"), post-signature filing trailers (press releases,
About-Company, forward-looking statements, contact blocks). See
`task_rules/scope_rule.md` for the structural rule.

## Rubric: nesting depth 0–7

`level` in each JSONL record is the clause's nesting depth in the
agreement's structural hierarchy. Each record also carries `order`
— a 0-indexed sequence number within the idx, document order — so
consumers can reconstruct the linear sequence without relying on
JSON key ordering. Standalone agreement (no subdocuments):

  - Depth 0  →  the agreement **title alone** (exactly one record per
                idx). The preamble is NOT depth 0.
  - Depth 1  →  every direct child of the agreement: the preamble
                paragraph, the recitals block, **each top-level body
                clause** (Article when the doc uses Article/Section
                nesting, otherwise the numbered Section), and the
                signature block.
  - Depth 2  →  direct children of L1 clauses: Section under Article,
                or lettered "(a)/(b)/(c)" subsection under a top
                Section.
  - Depth 3  →  direct children of L2 clauses: lettered "(a)" under
                Section-under-Article, or roman/letter/digit items
                "(i)/(A)/(1)" under "(a)".
  - Depth 4+ →  deeper nesting.

Subdocument penalty: each attached subdocument (cls=exhibit/schedule/
appendix/annex with descriptive title, NOT the SEC envelope) adds +1
to every descendant's depth. Ceiling 7. See
`task_rules/level_rubric.md` for full details and worked examples.

If the document is structurally flat (a one-page release, a unilateral
designation, a form with numbered fields), the deeper depths may
simply not appear. That is correct — depths capture structure that
exists, don't synthesise depth that doesn't.

## Source of truth and reconstruction

`data/auto_parse/parse_source_of_truth.jsonl` — bs4 plain-text
extraction of each source HTML. The reconstruction test:
concat-of-spans for one idx ≈ `span_clean[idx]`.

`scripts/measure_reconstruction.py` computes per-idx word coverage
and char ratio. The freeze step **refuses** the freeze if word
coverage for this idx is below 90% (project bar from
`docs/DECISIONS.md` §10). A reconstruction failure is on equal
footing with a rubric violation: fix the parser so spans recover
the source words, then re-run the parser and freeze again.

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

1. Read the source-of-truth excerpt and the current parser output
   above. Decide the minimal edit that makes the parse reconstruct
   the source under the rubric.

2. Edit `clause-extract/scripts/parse_doc2dict_with_config.py` and/or
   `clause-extract/src/clause_extract/agreement_config.py`. Keep edits
   minimal — adjust ONE pattern or rule.

3. Re-run the parser on idxs 0..{current_idx} (ABSOLUTE paths):

     cd {repo_dir_abs} && \
       uv run scripts/parse_doc2dict_with_config.py \
         --limit {limit_n} \
         --output-dir {data_dir_abs} \
         --no-truncate

4. Verify the JSONL at
   `{data_dir_abs}/parse_doc2dict_with_config_nodes.jsonl` for
   idx={current_idx} matches the rubric. `ls -la` it first to confirm
   the mtime is recent — a stale jsonl would silently freeze wrong data.

5. Investigate reconstruction quality:

     uv run scripts/measure_reconstruction.py --idx {current_idx}

   Read the word coverage and char ratio for idx={current_idx}. **Word
   coverage < 90% is a HARD FAIL at freeze time** (per
   `docs/DECISIONS.md` §10). If you're below 90%, the parser dropped
   real content; find the missing words in the source, locate the
   record(s) that should have captured them, and fix the slicing.

6. Freeze the corrected output:

     uv run scripts/level_loop/freeze.py {current_idx}

   freeze.py validates the rubric (exactly 1 depth-0 — the title
   alone, max depth ≤ 7, no envelope as first record, monotonic
   `order`, no filing-trailer phrases, no forbidden monkey-patch
   constructs in the parser source) AND enforces the **90%
   reconstruction gate**. Below 90% word coverage → freeze refused.
   The error message includes the percentage, char ratio, count of
   missing words, and a sample of the missing words; use that to
   localize what got dropped, then fix the parser slicing and retry.

7. Regression check:

     uv run scripts/level_loop/regress.py

   Failure here means your edit broke a previously frozen idx. Loop
   back to step 2 and find a smaller change.

8. Advance:

     uv run scripts/level_loop/advance.py

When advance succeeds, the turn is done. Print a one-line summary and
stop.

## Hard rules

- Edit ONLY `parse_doc2dict_with_config.py` and `agreement_config.py`.
- Never edit files under `data/auto_parse/level_freeze/frozen/` by hand.
- Never call `freeze.py --force` unless the loop driver tells you to.
- No document-class-specific code paths (`if is_government_contract`,
  `if is_unilateral`). The structural rules apply uniformly; if you
  feel the need to branch by document class, the rule itself needs
  rethinking.
- No phrase blocklists, no level capping, no trailer-keyword regexes —
  see `task_rules/scope_rule.md` "Forbidden approaches".
- If you cannot satisfy the rubric without breaking a frozen idx, STOP,
  print why, and exit. The human running the loop will decide.
```

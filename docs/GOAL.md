# The goal

> **Scope note.** This document is the project's goal-framing — written before the implementation scope was finalized. It mentions classification and statistical aggregation as eventual purposes the parsed corpus serves. Those purposes are explicitly **out of scope for this repo**. `clause-extract` produces structured parsed content with hierarchy and nuances. Anyone wanting to categorize, retrieve, or compute probabilities builds those layers separately. Read this document for context and motivation; read [`docs/SCHEMA.md`](SCHEMA.md) and [`docs/HANDOFF.md`](HANDOFF.md) for what to actually build.

## What this is

Across a corpus of SEC EX-10 exhibit agreements, compute reliable estimates of: **given a contract type, what's the probability it contains clause X?**

Examples of the kind of question this should eventually answer:

- "What fraction of indemnification agreements have a fee-shifting clause?"
- "Among gas gathering agreements, how often does ARTICLE 14 cover insurance?"
- "Of credit facility amendments, what percentage include change-of-control triggers?"

## What this is NOT

This is not:

- **A retrieval system.** Nobody is querying these clauses for a RAG pipeline.
- **A user-facing tool.** Lawyers aren't reading these parsed outputs.
- **An ingestion pipeline.** Nothing downstream is consuming this incrementally.

It's a **batch analytical job**. Run once across the corpus, produce statistics, possibly re-run when the corpus or taxonomy changes.

Clause text extraction is, however, an unavoidable intermediate step. We have to pull clause-level content out of each document to verify the parser is splitting things correctly and to feed whatever classification stage decides "is this an indemnification clause." So the pipeline produces clause text as a byproduct even though the deliverable is statistical.

## Why the framing matters

The "good enough" bar is set by **statistical reliability**, not per-document fidelity. If we miss 5% of indemnification clauses across 1,066 agreements, the resulting probability estimate is biased by 5% — annoying but recoverable. If we miss 5% of some specific agreement's clauses, that agreement's individual analysis is wrong, but no one is looking at individual analyses.

This changes what dependencies are worth their cost:

| Concern | Matters for retrieval/users | Matters for our goal |
| --- | --- | --- |
| Per-document parse perfection | high | low |
| Aggregate consistency across docs | medium | **high** |
| Auditable typing (regex vs ML) | high (legal review) | low (statistics) |
| Speed | low (cached) | medium (re-run cost) |
| Per-element provenance / debugging logs | medium | low |
| Handling of edge-case formats | high | medium (just exclude them) |

The implication: **methods that produce noisy-but-uniform output are better than methods that produce perfect-but-non-uniform output**. We can correct for systematic bias (everything misses indemnification 3% of the time → adjust). We can't correct for non-uniform bias (sometimes the parser produces granular sections, sometimes blocky ones, depending on the document's styling).

## What "deterministically split the clauses" means in this context

When Arthur said "deterministic," he didn't mean "regex over ML" in the legal-defensibility sense — that would matter for a clause-presence claim made to a client. He meant **reproducible**: the same input produces the same output, no model drift, no probabilistic decisions in the parser. Whatever we do, two runs on the same document must produce identical clause boundaries. Otherwise the statistics drift between runs and the analysis loses meaning.

Both `doc2dict` and the regex path of `lexnlp.nlp.en.segments.sections` are deterministic in this sense; `sec-parser` is too but turned out not to fit. The "classify later" stage is where the determinism question gets harder, because if classification uses an LLM, two runs will disagree at the margins. This is a real consideration but not a blocker — there are deterministic classification methods (rule-based, embedding+cluster with fixed seeds, simple text classifiers).

## The constraint we don't have but should respect

We don't have a labeled training set. There's no ground truth for "this section IS an indemnification clause." Any classification we build is unsupervised or weakly-supervised. This rules out training a supervised classifier on labeled clause data unless we generate that data first (probably via LLM, with the determinism caveat above).

## Methodology

We can't evaluate "is the parser good enough" against the abstract goal — we need a comparison artifact. The plan is iterative, sample-driven, cumulative, and grounded in a hand-built reference rather than a labeled training set.

**Step 1 — Build the reference, in growing batches.** Claude manually extracts the clauses from samples 1–10. This is the ground truth for iteration 1. Not a training set, not labeled data in the supervised-learning sense — just a reference of what the right answer looks like for these specific documents, used as the comparand for every parser configuration we try. Each successful iteration extends the reference: 1–10, then 1–20, then 1–30, and so on.

**Step 2 — Run both arms on every sample in the current cumulative set.**

- Arm A: `doc2dict` with no special configuration (defaults).
- Arm B: `doc2dict` with a custom configuration (the configuration we are tinkering with on this iteration).

**Step 3 — Compute two scores against Claude's manual extraction.**

- **Marginal accuracy**: the arm's accuracy on the *new* 10 samples added in this iteration.
- **Aggregate accuracy**: the arm's accuracy across *all* samples extracted so far (1..N).

**Step 4 — Decision gate.**

- **Both marginal > 90% AND aggregate > 90%** → **Successful.** Record the row (see Step 5). Add 10 more samples (extend the reference). Iterate.
- **Marginal == 100% but the aggregate >90% gate is not met** → record the row with the config and the `[idx]` of this attempt for posterity (we want this artifact — a config that nailed a fresh 10 is a data point worth keeping even if it regressed against earlier batches), but **treat the iteration as unsuccessful otherwise**. Tinker with the config and re-run Step 2 on the same cumulative set.
- **Anything else** → **Unsuccessful.** Tinker with the config and re-run Step 2 on the same cumulative set. Do not expand to a new batch until the iteration passes.

**Step 5 — Record every iteration as a JSONL row, regardless of outcome.**

```json
{
  "iteration": 3,
  "samples_aggregate": [1, 2, "...", 30],
  "samples_marginal": [21, 22, "...", 30],
  "content_without_custom_config": {"<sample_idx>": "<doc2dict default output>"},
  "content_with_config_x":         {"<sample_idx>": "<doc2dict custom-config output>"},
  "configs_used": "<the custom config that produced content_with_config_x>",
  "marginal_accuracy": 1.0,
  "aggregate_accuracy": 0.87,
  "outcome": "marginal_only"
}
```

`outcome` is one of `"success"`, `"marginal_only"`, or `"unsuccessful"`. The unsuccessful and marginal-only rows are as important as the successful ones — they're how we recognize a regression pattern later.

This log is the audit trail. It lets us:

- Reproduce any past iteration without re-deriving what config we ran.
- Diff configurations across iterations to see what change caused which behavior shift.
- Recover if conversation context is lost mid-iteration — the JSONL is the durable artifact, not the chat.

**Why this shape.** The loop is deliberately small (10-at-a-time growth) because manual extraction is the bottleneck. Cumulative scoring is what catches regressions: a config that handles batch 3 but breaks batch 1 is not actually generalizing — it's overfitting to whatever the new documents happen to look like. The dual gate (marginal AND aggregate) is what forces the parser to be uniformly good across the whole reference set, which is exactly what the statistical-bias argument earlier in this doc demands.

## What "success" looks like

A pipeline that:

1. Ingests the 1,066-row HF dataset (or whatever it grows to)
2. Produces a tabular dataset where each row is `(agreement_id, clause_type, present_bool, [optional: clause_text])`
3. Allows aggregating that table to compute `P(clause_type | agreement_type)` and similar conditional probabilities
4. Can be re-run end-to-end deterministically
5. Documents its known biases (e.g., "image-only docs excluded; n=940 of 1066")

Bonus: produces stable enough output that adding 1,000 new agreements doesn't shift the probability estimates by more than sampling noise.

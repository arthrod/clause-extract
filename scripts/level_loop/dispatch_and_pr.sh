#!/usr/bin/env bash
# Fire one level-loop dispatch and, on successful advance, open a stacked
# PR for the new freeze.
#
# Per-idx flow:
#   1. Pre-flight: working tree clean + regress.py passes.
#   2. Determine TARGET_IDX from state.json.
#   3. Branch loop/idx-<TARGET_IDX> off whatever branch is currently
#      checked out (the prior PR head).
#   4. Run scripts/level_loop/turn.sh (one opencode dispatch, expected
#      to advance state by exactly one).
#   5. Verify state.current_idx advanced from TARGET_IDX to TARGET_IDX+1
#      (an advance of more than one is treated as overshoot and fails).
#   6. Stage state.json + frozen/idx_<N>.jsonl + prompt snapshots +
#      any parser/config edits the agent made.
#   7. Commit, push the new branch, gh pr create --base <prior> --head
#      loop/idx-<N>.
#
# Failures leave the branch in place for inspection; nothing is pushed.
#
# Usage:
#   scripts/level_loop/dispatch_and_pr.sh
#   MODEL=anthropic/claude-sonnet-4-6 scripts/level_loop/dispatch_and_pr.sh
#
# Env:
#   MODEL — opencode model id, default zai-coding-plan/glm-5.1.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

DATA_DIR="$REPO_ROOT/data/auto_parse"
STATE="$DATA_DIR/level_freeze/state.json"
FROZEN_DIR="$DATA_DIR/level_freeze/frozen"
TURNS_DIR="$DATA_DIR/level_freeze/turns"

MODEL="${MODEL:-zai-coding-plan/glm-5.1}"
export MODEL

# Helper: read state.current_idx
read_current_idx() {
  uv run --quiet python - <<PY
import json
print(json.load(open("$STATE"))["current_idx"])
PY
}

# Helper: render level distribution for an idx as a one-line summary
render_level_dist() {
  local idx="$1"
  uv run --quiet python - <<PY
import json
from collections import Counter
recs = [json.loads(l) for l in open("$FROZEN_DIR/idx_${idx}.jsonl")]
c = Counter(r["level"] for r in recs)
levels = dict(sorted(c.items()))
print(f"{len(recs)} records, levels={levels}")
PY
}

# Helper: pull the L0 record head for an idx
render_l0_head() {
  local idx="$1"
  uv run --quiet python - <<PY
import json
for line in open("$FROZEN_DIR/idx_${idx}.jsonl"):
    r = json.loads(line)
    if r.get("level") == 0:
        head = (r.get("span") or "")[:160].replace(chr(10), " | ")
        print(head)
        break
PY
}

# 1. Read TARGET_IDX
if [[ ! -f "$STATE" ]]; then
  echo "ERROR: state.json not found at $STATE" >&2
  exit 2
fi
TARGET_IDX="$(read_current_idx)"

PRIOR_BRANCH="$(git branch --show-current)"
NEW_BRANCH="loop/idx-${TARGET_IDX}"

echo "=== dispatch_and_pr ==="
echo "  target idx:   ${TARGET_IDX}"
echo "  prior branch: ${PRIOR_BRANCH}"
echo "  new branch:   ${NEW_BRANCH}"
echo "  MODEL:        ${MODEL}"

# 2. Pre-flight: clean working tree
if [[ -n "$(git status --porcelain)" ]]; then
  echo "ERROR: working tree dirty; refuse to dispatch with uncommitted changes" >&2
  git status --short >&2
  exit 2
fi

# 3. Pre-flight: regress passes against current frozen state
if ! uv run scripts/level_loop/regress.py >/dev/null 2>&1; then
  echo "ERROR: regress.py fails before dispatch — fix existing state first" >&2
  uv run scripts/level_loop/regress.py 2>&1 | tail -20 >&2
  exit 2
fi

# 4. Pre-flight: branch must not already exist
if git show-ref --verify --quiet "refs/heads/${NEW_BRANCH}"; then
  echo "ERROR: branch ${NEW_BRANCH} already exists locally — checkout it manually if you want to retry" >&2
  exit 2
fi
if git ls-remote --exit-code --heads origin "${NEW_BRANCH}" >/dev/null 2>&1; then
  echo "ERROR: branch ${NEW_BRANCH} already exists on origin" >&2
  exit 2
fi

# 5. Pre-flight: PRIOR_BRANCH must be a real named branch
if [[ "$PRIOR_BRANCH" == "HEAD" || -z "$PRIOR_BRANCH" ]]; then
  echo "ERROR: not on a named branch (current=${PRIOR_BRANCH}); refuse to PR from detached state" >&2
  exit 2
fi

# 6. Create new branch
git checkout -b "$NEW_BRANCH"

# 7. Fire turn.sh
echo
echo "=== firing turn.sh for idx=${TARGET_IDX} ==="
./scripts/level_loop/turn.sh

# 8. Verify advance
NEW_IDX="$(read_current_idx)"
echo
echo "=== post-dispatch ==="
echo "  state.current_idx: ${TARGET_IDX} -> ${NEW_IDX}"

if [[ "$NEW_IDX" -le "$TARGET_IDX" ]]; then
  echo "DISPATCH FAILED: state.current_idx is ${NEW_IDX} (expected > ${TARGET_IDX})" >&2
  echo "leaving branch ${NEW_BRANCH} in place; inspect with:" >&2
  echo "  git status; git log --oneline ${PRIOR_BRANCH}..HEAD" >&2
  exit 3
fi

if [[ "$NEW_IDX" -ne "$((TARGET_IDX + 1))" ]]; then
  echo "ERROR: overshoot — current_idx jumped from ${TARGET_IDX} to ${NEW_IDX}, expected exactly +1" >&2
  echo "leaving branch ${NEW_BRANCH} in place for review" >&2
  exit 4
fi

# 9. Verify the freeze file actually exists and regress still passes
if [[ ! -f "${FROZEN_DIR}/idx_${TARGET_IDX}.jsonl" ]]; then
  echo "ERROR: state advanced but frozen/idx_${TARGET_IDX}.jsonl is missing" >&2
  exit 5
fi
if ! uv run scripts/level_loop/regress.py >/dev/null 2>&1; then
  echo "ERROR: post-dispatch regress.py fails — fresh freeze is inconsistent" >&2
  uv run scripts/level_loop/regress.py 2>&1 | tail -20 >&2
  exit 5
fi

# 10. Compute level distribution and L0 head
LEVEL_DIST="$(render_level_dist "$TARGET_IDX")"
L0_HEAD="$(render_l0_head "$TARGET_IDX")"

# Did the agent edit parser source?
PARSER_EDITED="no"
if ! git diff --quiet HEAD -- scripts/parse_doc2dict_with_config.py src/clause_extract/agreement_config.py 2>/dev/null; then
  PARSER_EDITED="yes"
fi

# 11. Stage everything the dispatch produced
git add "$STATE"
git add "${FROZEN_DIR}/idx_${TARGET_IDX}.jsonl"
git add "${TURNS_DIR}/prompt_idx${TARGET_IDX}_"*.md 2>/dev/null || true
# The canonical jsonl is regenerated by every parser run; commit it
# alongside the freeze so the PR reflects the parser output that
# produced the new baseline.
git add "$DATA_DIR/parse_doc2dict_with_config_nodes.jsonl" 2>/dev/null || true
git add scripts/parse_doc2dict_with_config.py src/clause_extract/agreement_config.py 2>/dev/null || true

if [[ -z "$(git diff --cached --name-only)" ]]; then
  echo "ERROR: nothing staged for commit even though state advanced — investigate" >&2
  exit 5
fi

# 12. Commit. Build the message via heredoc to a tmp file so quoting stays simple.
COMMIT_MSG_FILE="$(mktemp)"
cat >"$COMMIT_MSG_FILE" <<EOF
freeze idx=${TARGET_IDX}: ${LEVEL_DIST}

Stacked over ${PRIOR_BRANCH}. Auto-committed by dispatch_and_pr.sh
after the agent dispatch advanced state.current_idx from ${TARGET_IDX}
to ${NEW_IDX}.

L0 head: ${L0_HEAD}

Parser edited during dispatch: ${PARSER_EDITED}.

regress.py passes for all $((NEW_IDX)) frozen idxs (0..${TARGET_IDX}).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF

git commit -F "$COMMIT_MSG_FILE"
rm -f "$COMMIT_MSG_FILE"

# 13. Push
git push -u origin "$NEW_BRANCH"

# 14. Open stacked PR. Body via heredoc to a tmp file.
PR_BODY_FILE="$(mktemp)"
PARSER_LINE=""
if [[ "$PARSER_EDITED" == "yes" ]]; then
  PARSER_LINE="- Parser/config edits the agent made (see diff)."
fi

cat >"$PR_BODY_FILE" <<EOF
## Summary

Stacked over \`${PRIOR_BRANCH}\`. Agent dispatch advanced state from idx=${TARGET_IDX} to idx=${NEW_IDX}.

\`${LEVEL_DIST}\`

L0 head:

> ${L0_HEAD}

Parser source edited during dispatch: **${PARSER_EDITED}**.

regress.py passes for all $((NEW_IDX)) frozen idxs (0..${TARGET_IDX}).

## What is in this PR

- \`data/auto_parse/level_freeze/state.json\` — current_idx and frozen list updated to ${NEW_IDX}.
- \`data/auto_parse/level_freeze/frozen/idx_${TARGET_IDX}.jsonl\` — new baseline.
- Prompt snapshot(s) for this idx — forensic record of what the agent saw.
${PARSER_LINE}

🤖 Generated by \`scripts/level_loop/dispatch_and_pr.sh\`
EOF

# Trim title to under 70 chars
PR_TITLE_BASE="freeze idx=${TARGET_IDX}"
PR_TITLE_TAIL="$(echo "$LEVEL_DIST" | sed 's/, levels=.*//')"
PR_TITLE="${PR_TITLE_BASE}: ${PR_TITLE_TAIL}"

gh pr create \
  --base "$PRIOR_BRANCH" \
  --head "$NEW_BRANCH" \
  --title "$PR_TITLE" \
  --body-file "$PR_BODY_FILE"

rm -f "$PR_BODY_FILE"

echo
echo "=== DONE ==="
echo "  idx=${TARGET_IDX} frozen, PR opened against ${PRIOR_BRANCH}."
echo "  next dispatch target: idx=${NEW_IDX}"
echo "  next branch will be:  loop/idx-${NEW_IDX}"

#!/usr/bin/env bash
# Run one fresh-context turn of the level-tuning loop via opencode.
#
# Each invocation: regenerates the prompt (snapshotted into a tmp file
# alongside the run), pipes it into `opencode run`, and prints the new
# state at the end so the orchestrator can decide whether to fire
# another turn.
#
# `opencode run` creates a NEW session per call → that's the "fresh
# context" the design relies on. Re-running this script is the loop.
#
# Usage:
#   scripts/level_loop/turn.sh
#   MODEL=anthropic/claude-sonnet-4-5 scripts/level_loop/turn.sh
#   DRY_RUN=1 scripts/level_loop/turn.sh   # just print the prompt, don't dispatch

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DATA_DIR="$(cd "$REPO_ROOT" && pwd)/data/auto_parse"
LOG_DIR="$DATA_DIR/level_freeze/turns"
mkdir -p "$LOG_DIR"

# Sanity gate: REPO_ROOT must contain the parser the dispatch is about to
# tune. Earlier turns silently dispatched into a now-deleted nested
# `clause-extract/clause-extract/` submodule path that opencode had cached
# in ~/.local/share/opencode/opencode.db; the agent loaded into an empty
# directory and exited confused. Fail loudly here instead.
PARSER="$REPO_ROOT/scripts/parse_doc2dict_with_config.py"
if [[ ! -f "$PARSER" ]]; then
  echo "ERROR: parser not found at $PARSER — REPO_ROOT looks wrong." >&2
  echo "REPO_ROOT=$REPO_ROOT" >&2
  exit 2
fi

STATE="$DATA_DIR/level_freeze/state.json"
CUR_IDX="$(uv run --quiet python -c "import json; print(json.load(open('$STATE'))['current_idx'])")"

ts="$(date +%Y%m%dT%H%M%S)"
prompt_file="$LOG_DIR/prompt_idx${CUR_IDX}_$ts.md"
log_file="$LOG_DIR/run_idx${CUR_IDX}_$ts.log"

cd "$REPO_ROOT"
uv run scripts/level_loop/prompt.py > "$prompt_file"

echo "=== turn for idx=$CUR_IDX ==="
echo "prompt: $prompt_file ($(wc -c < "$prompt_file") bytes)"

if [[ "${DRY_RUN:-0}" = "1" ]]; then
  echo "DRY_RUN=1 — printing prompt and exiting."
  cat "$prompt_file"
  exit 0
fi

MODEL="${MODEL:-}"
model_arg=()
if [[ -n "$MODEL" ]]; then
  model_arg=(--model "$MODEL")
fi

echo "dispatching to opencode (log: $log_file)..."
# `--dangerously-skip-permissions` is needed because opencode's default
# permission model would prompt on every tool call, which can't work in a
# headless turn. The agent only has filesystem + bash access in the repo,
# which is the same surface a human running this script would have.
#
# `--dir "$REPO_ROOT"` forces opencode's working directory to the repo
# root regardless of any stale project pointer in `.git/opencode` or
# ~/.local/share/opencode/opencode.db (the project row's `worktree`
# column). Without this, opencode resolves cwd from the cached project
# entry which can lag a repo move and dispatch the agent into a ghost
# path. See the May 9 idx=14 attempt 2 failure for the original incident.
opencode run \
  --dir "$REPO_ROOT" \
  "${model_arg[@]}" \
  --dangerously-skip-permissions \
  --print-logs \
  --log-level INFO \
  --title "level-loop idx=$CUR_IDX $ts" \
  "$(cat "$prompt_file")" \
  2>&1 | tee "$log_file"

echo
echo "=== state after turn ==="
cat "$STATE"

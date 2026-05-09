#!/usr/bin/env bash
# Run N idxs through the level-tuning loop, with up to 3 retries per idx.
#
# Per-idx flow:
#   1. Snapshot parser source.
#   2. Dispatch one opencode session (turn.sh).
#   3. If state.current_idx advanced: success — clear attempts, move to next idx.
#   4. Else: record attempt diagnostics + monkey-patch hits + parser diff
#      (so the next dispatch's prompt knows what was tried). Retry.
#   5. After 3 failed retries: log "abandoning idx=N" and force-advance
#      state.current_idx so the loop continues with the next idx.
#
# This way an unattended overnight run gets through all N idxs even if a
# few are intractable, leaving abandoned idxs documented for offline review.
#
# Usage:
#   scripts/level_loop/run_n_turns.sh 10
#   MODEL=zai-coding-plan/glm-5.1 scripts/level_loop/run_n_turns.sh 10
#
# Env:
#   MAX_ATTEMPTS_PER_IDX (default 3) — retries per idx before abandoning.

set -euo pipefail

N="${1:-1}"
MAX_ATTEMPTS_PER_IDX="${MAX_ATTEMPTS_PER_IDX:-3}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DATA_DIR="$(cd "$REPO_ROOT" && pwd)/data/auto_parse"
STATE="$DATA_DIR/level_freeze/state.json"
ATTEMPTS_DIR="$DATA_DIR/level_freeze/attempts"
LOG_DIR="$DATA_DIR/level_freeze/turns"
DRIVER_LOG="$LOG_DIR/driver_$(date +%Y%m%dT%H%M%S).log"
PARSER="$REPO_ROOT/scripts/parse_doc2dict_with_config.py"

mkdir -p "$LOG_DIR" "$ATTEMPTS_DIR"
cd "$REPO_ROOT"

read_idx()      { uv run --quiet python -c "import json; print(json.load(open('$STATE'))['current_idx'])"; }
count_frozen()  { uv run --quiet python -c "import json; print(len(json.load(open('$STATE'))['frozen']))"; }

force_advance() {
  local from_idx="$1" reason="$2"
  uv run --quiet python - <<EOF
import json, time
p = "$STATE"
s = json.load(open(p))
s["current_idx"] = $from_idx + 1
s.setdefault("history", []).append({
    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
    "action": "force_advance",
    "from_idx": $from_idx,
    "to_idx": $from_idx + 1,
    "note": "$reason",
})
open(p, "w").write(json.dumps(s, indent=2) + "\n")
EOF
}

{
  echo "=== driver start: N=$N MODEL=${MODEL:-<default>} max_attempts=$MAX_ATTEMPTS_PER_IDX ==="
  echo "start state: current_idx=$(read_idx) frozen_count=$(count_frozen)"
  echo "log: $DRIVER_LOG"
  echo
} | tee -a "$DRIVER_LOG"

idxs_done=0
while [[ "$idxs_done" -lt "$N" ]]; do
  idx_at_start="$(read_idx)"
  attempt=1
  succeeded=0

  while [[ "$attempt" -le "$MAX_ATTEMPTS_PER_IDX" ]]; do
    snapshot="$ATTEMPTS_DIR/idx_${idx_at_start}_attempt${attempt}_snapshot.py"
    cp "$PARSER" "$snapshot"

    ts="$(date +%Y%m%dT%H%M%S)"
    echo "[$ts] idx=$idx_at_start attempt=$attempt/$MAX_ATTEMPTS_PER_IDX (idxs_done=$idxs_done/$N) — dispatching..." | tee -a "$DRIVER_LOG"

    # Dispatch one opencode session in the background, then watch state.json.
    # The agent's prompt instructs it to advance EXACTLY ONCE and exit. If
    # the agent ignores that and tries to batch the next idx, we kill the
    # opencode process as soon as state.current_idx advances past
    # idx_at_start + 1 — the guarantee is one-advance-per-dispatch.
    ./scripts/level_loop/turn.sh >/dev/null 2>&1 &
    TURN_PID=$!

    # Watch loop: poll state every 5s, kill turn.sh if overshoot detected.
    while kill -0 "$TURN_PID" 2>/dev/null; do
      idx_now="$(read_idx)"
      if [[ "$idx_now" -gt $((idx_at_start + 1)) ]]; then
        echo "[$(date +%H:%M:%S)] OVERSHOOT detected — agent advanced past idx=$((idx_at_start + 1)). Killing turn.sh ($TURN_PID) to enforce one-advance-per-dispatch." | tee -a "$DRIVER_LOG"
        kill "$TURN_PID" 2>/dev/null || true
        sleep 2
        pkill -P "$TURN_PID" 2>/dev/null || true
        pkill -f 'opencode run' 2>/dev/null || true
        # Roll back any over-batched idxs (keep idx_at_start+1, drop the rest).
        # Each over-batch covers exactly one idx beyond the legitimate target.
        for over_idx in $(seq $((idx_at_start + 2)) "$idx_now"); do
          rm -f "$DATA_DIR/level_freeze/frozen/idx_${over_idx}.jsonl"
          echo "[$(date +%H:%M:%S)] rolled back overshoot freeze for idx=$over_idx" | tee -a "$DRIVER_LOG"
        done
        # Reset state so frozen reflects only the legitimate advance, and
        # current_idx is the first un-handled idx.
        uv run --quiet python - <<EOF
import json, time
p = "$STATE"
s = json.load(open(p))
s["frozen"] = [i for i in s["frozen"] if i <= $idx_at_start + 1]
s["current_idx"] = $idx_at_start + 2
s.setdefault("history", []).append({
    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
    "action": "overshoot_rollback",
    "kept_idx": $idx_at_start + 1,
    "rolled_back_from": "$idx_now",
    "note": "agent batched additional idxs past $((idx_at_start + 1)); driver killed and rolled back"
})
open(p, "w").write(json.dumps(s, indent=2) + "\n")
EOF
        break
      fi
      sleep 5
    done
    wait "$TURN_PID" 2>/dev/null || true

    idx_after="$(read_idx)"
    if [[ "$idx_after" -gt "$idx_at_start" ]]; then
      echo "[$(date +%H:%M:%S)] idx=$idx_at_start ADVANCED $idx_at_start → $idx_after on attempt $attempt" | tee -a "$DRIVER_LOG"
      uv run scripts/level_loop/clear_attempts.py "$idx_at_start" >/dev/null 2>&1 || true
      succeeded=1
      break
    fi

    # Did not advance — record what was tried.
    uv run scripts/level_loop/record_attempt.py \
        "$idx_at_start" "$attempt" \
        --snapshot "$snapshot" \
        --outcome "no advance after dispatch" 2>&1 | tee -a "$DRIVER_LOG" || true

    attempt=$((attempt + 1))
  done

  if [[ "$succeeded" -ne 1 ]]; then
    echo "[$(date +%H:%M:%S)] idx=$idx_at_start ABANDONED after $MAX_ATTEMPTS_PER_IDX attempts — force-advancing" | tee -a "$DRIVER_LOG"
    force_advance "$idx_at_start" "force_advance: ${MAX_ATTEMPTS_PER_IDX}/${MAX_ATTEMPTS_PER_IDX} attempts failed; idx not frozen"
    uv run scripts/level_loop/clear_attempts.py "$idx_at_start" >/dev/null 2>&1 || true
  fi

  idxs_done=$((idxs_done + 1))
done

echo "=== driver done: $idxs_done idxs processed ===" | tee -a "$DRIVER_LOG"
echo "final: current_idx=$(read_idx) frozen=$(count_frozen)" | tee -a "$DRIVER_LOG"

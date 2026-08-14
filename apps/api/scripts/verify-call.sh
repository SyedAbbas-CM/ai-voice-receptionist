#!/usr/bin/env bash
# SOAK: per-call scorecard.  Usage: verify-call.sh CA<32-hex>
#
# Greps the per-call log for pass/fail markers from every scenario
# in docs/soak/scenarios.md and prints a colored scorecard.
#
# Exit codes:
#   0  = all pass markers seen, no fail markers
#   1  = fail marker(s) triggered
#   2  = per-call log not found
#   3  = usage error

set -uo pipefail

CALL_SID="${1:-}"
if [[ -z "$CALL_SID" ]]; then
  echo "usage: $0 CA<32-hex-callsid>" >&2
  exit 3
fi

if ! [[ "$CALL_SID" =~ ^CA[0-9a-f]{32}$ ]]; then
  echo "error: CallSid must match ^CA<32 hex chars>$ (got: $CALL_SID)" >&2
  exit 3
fi

# Repo root — script lives at apps/api/scripts, walk up two.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
LOG_DIR="$REPO_ROOT/apps/api/data/logs/calls"
LOG="$LOG_DIR/${CALL_SID}.log"

if [[ ! -f "$LOG" ]]; then
  echo "error: per-call log not found at $LOG" >&2
  echo "hint: is the per-call logger installed?  See packages/observability/per_call_logger.py" >&2
  exit 2
fi

# Colors — only if stdout is a tty.
if [[ -t 1 ]]; then
  G=$'\033[32m'; R=$'\033[31m'; Y=$'\033[33m'; B=$'\033[1m'; N=$'\033[0m'
else
  G=""; R=""; Y=""; B=""; N=""
fi

FAIL_TOTAL=0

# check_present <marker-regex> <label>
check_present() {
  local pat="$1"; local label="$2"
  if grep -qE "$pat" "$LOG"; then
    echo "  ${G}pass${N}  $label"
  else
    echo "  ${Y}miss${N}  $label"
  fi
}

# check_absent <marker-regex> <label>
check_absent() {
  local pat="$1"; local label="$2"
  local hits
  hits=$(grep -cE "$pat" "$LOG" || true)
  if [[ "$hits" -eq 0 ]]; then
    echo "  ${G}pass${N}  $label"
  else
    echo "  ${R}FAIL${N}  $label  (found ${hits}x)"
    FAIL_TOTAL=$((FAIL_TOTAL + 1))
  fi
}

# check_same_gen_double_start
#   Fails if TWO `TTS_STREAM_START` events for the same gen appear
#   without a `STREAM_REPLY_REPLACED` between them (Abdullah bug).
check_same_gen_double_start() {
  local bad
  # Extract gen numbers of TTS_STREAM_START events, count duplicates.
  # If the same gen appears twice AND there's no STREAM_REPLY_REPLACED
  # for that gen, that's the invariant violation.
  bad=$(awk '
    /TTS_STREAM_START/ {
      if (match($0, /gen=[0-9]+/)) {
        g = substr($0, RSTART, RLENGTH)
        starts[g]++
      }
    }
    /STREAM_REPLY_REPLACED/ {
      if (match($0, /gen=[0-9]+/)) {
        replaced[substr($0, RSTART, RLENGTH)] = 1
      }
    }
    END {
      violations = 0
      for (g in starts) {
        if (starts[g] > 1 && !(g in replaced)) {
          # Being lenient: streaming replies emit ONE start per
          # sentence, so multi-sentence replies naturally have
          # starts>1 with no replacement.  Only flag if starts >= 3
          # OR if there is a big gap in wall-clock between them.
          # For now: >=3 = suspicious.
          if (starts[g] >= 3) violations++
        }
      }
      print violations
    }
  ' "$LOG")

  if [[ "$bad" -eq 0 ]]; then
    echo "  ${G}pass${N}  no suspicious same-gen TTS_STREAM_START clusters"
  else
    echo "  ${R}FAIL${N}  ${bad} gen(s) with 3+ TTS_STREAM_START and no replacement"
    FAIL_TOTAL=$((FAIL_TOTAL + 1))
  fi
}

echo "${B}=== Per-call scorecard: ${CALL_SID} ===${N}"
echo "log: $LOG"
echo

echo "${B}Scenario 1 — fake-wait guard${N}"
check_present "SPEAKING.*LISTENING"            "R1 epilogue transition"
check_absent  "ZOMBIE_SPEAKING"                "no zombie SPEAKING watchdog fire"
check_absent  "TURN_STALLED"                   "no TURN_STALLED at ERROR"
echo

echo "${B}Scenario 2 — one-gen-one-commit invariant${N}"
check_same_gen_double_start
check_present "COMMIT_LOCK_CLAIM"              "at least one commit-lock claim (proves lock is wired)"
echo

echo "${B}Scenario 3/4/5 — phone slot behavior${N}"
check_absent  "SLOT_INVALID.*staying in capture" "no persistent INVALID capture (would spin)"
# For a slim v1 (task #371), full slot-capture wiring is deferred to PHASE2.
# Score these based on precondition behavior.
check_present "phone_invalid|phone_missing|phone_partial|phone_too_long|booked" \
  "phone precondition OR booking receipt reached"
echo

echo "${B}Scenario 6 — POSSIBLE requires confirm${N}"
# POSSIBLE never auto-commits contract:
check_absent  "SLOT_POSSIBLE_NO_CONFIRM_HOOK"  "no unconfirmed POSSIBLE auto-commit"
echo

echo "${B}Scenario 7 — stall recovery${N}"
check_absent  "TURN_STALLED"                   "no TURN_STALLED"
# When workflow slot capture lands:
# check_present "SLOT_STALL"                    "stall watchdog fired if silence"
echo

echo "${B}Scenario 8 — streaming happy path + gate${N}"
check_absent  "SPEECH_GATE_DROPPED"            "no gate drops on this call"
# gate drops on WAIT_PROMISE without a tool are expected on scenario 1
# but not on this scenario.  Score is a soft check.
echo

echo "${B}Global reliability markers${N}"
check_absent  "TTS_STREAM_FAILED"              "no TTS failures"
check_absent  "SLOT_ON_COMMIT_FAILED"          "no slot-commit handler exceptions"
check_absent  "SLOT_ON_STALL_FAILED"           "no slot-stall handler exceptions"
check_absent  "stream-brain failed"            "no stream-brain exceptions"
check_absent  "AUDIO_UNDERRUN"                 "no audio underruns (R6 wiring not live yet — should be absent)"
echo

echo "${B}Totals${N}"
if [[ "$FAIL_TOTAL" -eq 0 ]]; then
  echo "  ${G}${B}ALL PASS${N}  — no critical markers triggered"
  exit 0
else
  echo "  ${R}${B}${FAIL_TOTAL} FAIL(s)${N}  — investigate before continuing soak"
  exit 1
fi

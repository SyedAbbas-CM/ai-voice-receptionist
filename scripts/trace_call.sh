#!/usr/bin/env bash
# trace_call.sh — pull a call's full trace (transcript + events) via SSH
#
# When the /trace HTTP endpoint isn't reachable (no admin token, no
# tenant API key), fall back to raw sqlite dump. Same underlying data.
#
# Usage:
#   ./scripts/trace_call.sh CA<sid>
#   ./scripts/trace_call.sh --recent 5    # list 5 most recent CallSids

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIGHTSAIL_PEM="${LIGHTSAIL_PEM:-$REPO/LightsailDefaultKey-us-east-1.pem}"
LIGHTSAIL_HOST="${LIGHTSAIL_HOST:-3.227.16.73}"
LIGHTSAIL_USER="${LIGHTSAIL_USER:-ubuntu}"

# Wrap ssh in a function so the PEM path (which may contain spaces)
# stays quoted through argument expansion.
run_ssh() {
    ssh -i "$LIGHTSAIL_PEM" -o BatchMode=yes -o ConnectTimeout=8 \
        "${LIGHTSAIL_USER}@${LIGHTSAIL_HOST}" "$@"
}

if [ "${1:-}" = "--recent" ]; then
    N="${2:-5}"
    run_ssh "python3 -c '
import sqlite3
c = sqlite3.connect(\"/home/ubuntu/receptionist-agent/data/voiceops.db\")
for r in c.execute(\"SELECT id, tenant_id, started_at, status FROM sessions ORDER BY started_at DESC LIMIT $N\").fetchall():
    print(f\"{r[0]}  tenant={r[1]}  started={r[2]}  status={r[3]}\")
'"
    exit 0
fi

CALL_ID="${1:?usage: $0 <CallSid> | --recent [N]}"

# Normalise — accept either raw CA... or twilio_CA...
if [[ "$CALL_ID" == twilio_* ]]; then
    SESSION_ID="$CALL_ID"
else
    SESSION_ID="twilio_$CALL_ID"
fi

cat <<HEADER
═══════════════════════════════════════════════════════════════════════
Trace for $SESSION_ID
═══════════════════════════════════════════════════════════════════════
HEADER

# Ship a temp Python script + execute (avoids zsh heredoc parsing issues)
PYCODE=$(cat <<PYEOF
import sqlite3, json
from datetime import datetime, timezone

SID = "$SESSION_ID"

# ─── Chronological transcript from voiceops.transcript ───
print("── Transcript (voiceops.transcript) ──")
try:
    v = sqlite3.connect("/home/ubuntu/receptionist-agent/data/voiceops.db")
    rows = v.execute(
        "SELECT role, text, tool_name, timestamp FROM transcript "
        "WHERE session_id = ? ORDER BY timestamp ASC", (SID,)
    ).fetchall()
    if not rows:
        print("  (no transcript rows found)")
    for role, text, tool, ts in rows:
        role_tag = "CALLER" if role == "user" else "AGENT" if role == "assistant" else role.upper()
        tool_bit = f" [tool={tool}]" if tool else ""
        # Trim ts to HH:MM:SS
        t = str(ts).split(".")[0].split(" ")[-1] if ts else "?"
        print(f"  [{t}] {role_tag}{tool_bit}: {text!r}")
except Exception as e:
    print(f"  ERROR: {e}")

print()
print("── Call events (data/call_events.db) ──")
try:
    c = sqlite3.connect("/home/ubuntu/receptionist-agent/data/call_events.db")
    rows = c.execute(
        "SELECT wall_ts, turn_generation, source, kind, payload, error_category "
        "FROM call_events WHERE call_id = ? ORDER BY rowid ASC", (SID,)
    ).fetchall()
    if not rows:
        print("  (no events found)")
    kind_counts = {}
    for wall_ts, gen, source, kind, payload_raw, err in rows:
        kind_counts[kind] = kind_counts.get(kind, 0) + 1
        try:
            p = json.loads(payload_raw) if payload_raw else {}
        except Exception:
            p = {"raw": payload_raw[:80] if payload_raw else ""}
        t = datetime.fromtimestamp(wall_ts, tz=timezone.utc).strftime("%H:%M:%S") if wall_ts else "?"
        summary = ""
        if source == "stt" and kind == "final":
            summary = f"CALLER: {p.get('text','')!r}"
        elif source == "tts" and kind == "utterance":
            summary = f"AGENT-tts: {p.get('text','')!r}"
        elif source == "llm" and kind == "reply":
            reply = p.get("reply", "")
            summary = f"AGENT-llm: {reply[:120]!r}"
        elif err:
            summary = f"ERROR err={err} payload={json.dumps(p)[:120]}"
        elif "slot_capture" in kind or "policy_decision" in kind or "service_resolution" in kind:
            summary = f"HUMANNESS: {json.dumps(p)[:200]}"
        elif "empty" in kind.lower() or "fallback" in kind.lower():
            summary = f"REGRESSION-SIGNAL: {json.dumps(p)[:200]}"
        else:
            summary = f"({source}/{kind})"
        print(f"  [{t}] gen{gen} {summary}")

    print()
    print("── Event kind counts ──")
    for k, n in sorted(kind_counts.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {n:>4}  {k}")

    print()
    print("── Regression signal check ──")
    empty = kind_counts.get("empty_llm_completion", 0) + kind_counts.get("empty_completion", 0)
    fallback = kind_counts.get("empty_llm_deterministic_fallback", 0) + kind_counts.get("deterministic_fallback", 0)
    slot_enters = kind_counts.get("slot_capture_enter", 0)
    print(f"  empty_llm_completion:                {empty}   (want: 0)")
    print(f"  empty_llm_deterministic_fallback:    {fallback}   (want: 0)")
    print(f"  slot_capture_enter:                  {slot_enters}   (want: >=1 if Christiaan-style)")
except Exception as e:
    print(f"  ERROR: {e}")
PYEOF
)

echo "$PYCODE" | run_ssh 'cat > /tmp/_trace.py && python3 /tmp/_trace.py && rm /tmp/_trace.py'

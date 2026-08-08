"""Intelligence test runner — 7-turn adversarial scenario.

Tests every intelligence claim we've shipped:
  * Turn 1: task discovery + temporal ambiguity ('next Friday')
  * Turn 2: multi-slot extraction (name + phone in one utterance)
  * Turn 3: correction / evidence supersession (audit's exact test)
  * Turn 4: multi-intent (FAQ arrives mid-booking)
  * Turn 5: task resume + confirmation
  * Turn 6: out-of-scope refusal + task boundary
  * Turn 7: commit + verification + idempotency retry

For each turn:
  * Sends caller text to /chat/turn
  * Records agent reply
  * Generates ElevenLabs audio for the reply (saves WAV)
  * Snapshots the DialogueState from /debug/call/{id}
  * Captures any classified errors

Prints a compact per-turn report suitable for human review.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

import httpx


def _load_env(env_path: Path) -> None:
    """Load KEY=VALUE lines from .env into os.environ.  Doesn't
    require python-dotenv; strips inline comments + quotes."""
    if not env_path.exists():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        # Strip surrounding quotes
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        os.environ.setdefault(key, val)


_REPO_ROOT = Path(__file__).resolve().parents[1]
_load_env(_REPO_ROOT / ".env")

BASE = "http://127.0.0.1:8000"
ELEVEN_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVEN_VOICE = os.environ.get("ELEVENLABS_VOICE_ID") or "21m00Tcm4TlvDq8ikWAM"
OUT_DIR = Path("/tmp/intelligence-test")


SCRIPT = [
    {
        "id": "T1",
        "probe": "task discovery + temporal ambiguity",
        "caller": "Hi, I want to book a cleaning next Friday afternoon.",
        "expect_hints": [
            "asks caller which Friday (this vs next-week)",
            "OR proposes a specific Friday and lets caller correct",
        ],
    },
    {
        "id": "T2",
        "probe": "multi-slot extraction in one utterance",
        "caller": "Oh, this coming Friday. And it's for Sarah Khan, phone 555-0101.",
        "expect_hints": [
            "records caller_name=Sarah Khan, phone=+15550101, service=cleaning",
            "moves toward proposing a specific slot",
        ],
    },
    {
        "id": "T3",
        "probe": "correction / evidence supersession (audit acceptance test)",
        "caller": "Wait, actually make that Thursday afternoon, same time as before.",
        "expect_hints": [
            "reflects Thursday, NOT Friday, in the next agent turn",
            "kernel: Friday evidence SUPERSEDED; Thursday active",
        ],
    },
    {
        "id": "T4",
        "probe": "multi-intent: FAQ mid-booking",
        "caller": "Actually before we finalize, do you take Delta Dental?",
        "expect_hints": [
            "answers insurance question (Smile Dental accepts Delta Dental)",
            "does NOT lose the pending booking task",
        ],
    },
    {
        "id": "T5",
        "probe": "task resume + confirmation binding",
        "caller": "Great. Yeah, go ahead and book the Thursday appointment.",
        "expect_hints": [
            "resumes booking task",
            "goes to commit (Propose->Confirm->Commit)",
        ],
    },
    {
        "id": "T6",
        "probe": "out-of-scope refusal + task boundary",
        "caller": "One more thing — is my last cleaning covered by insurance?",
        "expect_hints": [
            "refuses (no access to billing) — offers callback",
            "does NOT mark booking as completed prematurely",
        ],
    },
    {
        "id": "T7",
        "probe": "final commit + verification",
        "caller": "No that's fine, just book it and we'll figure out the rest later.",
        "expect_hints": [
            "confirms with committed values (Thursday, cleaning, name, phone)",
            "durable event log shows commit outcome",
        ],
    },
]


def _post(path: str, payload: dict, timeout: float = 30.0) -> dict:
    with httpx.Client(base_url=BASE, timeout=timeout) as client:
        r = client.post(path, json=payload)
        r.raise_for_status()
        return r.json()


def _get(path: str, params: dict | None = None, timeout: float = 15.0) -> dict:
    with httpx.Client(base_url=BASE, timeout=timeout) as client:
        r = client.get(path, params=params or {})
        r.raise_for_status()
        return r.json()


def _synthesize_elevenlabs(text: str, out_path: Path) -> tuple[bool, float]:
    """Direct call to ElevenLabs.  Returns (ok, latency_ms).  Saves WAV
    on success.  Uses mp3 to keep bytes small — mp3 plays natively on
    any player."""
    if not ELEVEN_KEY:
        print(f"    [ElevenLabs disabled: no ELEVENLABS_API_KEY]")
        return False, 0.0
    url = (
        f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE}"
        f"?output_format=mp3_44100_128"
    )
    payload = {
        "text": text, "model_id": "eleven_turbo_v2_5",
        "voice_settings": {"stability": 0.55, "similarity_boost": 0.8,
                           "speed": 0.95, "style": 0.35,
                           "use_speaker_boost": True},
    }
    started = time.monotonic()
    try:
        with httpx.Client(timeout=45.0) as client:
            r = client.post(
                url,
                headers={"xi-api-key": ELEVEN_KEY,
                         "Content-Type": "application/json"},
                json=payload,
            )
            r.raise_for_status()
            out_path.write_bytes(r.content)
        latency_ms = (time.monotonic() - started) * 1000
        return True, latency_ms
    except Exception as e:
        print(f"    [ElevenLabs error: {e}]")
        return False, (time.monotonic() - started) * 1000


def _fetch_timeline(call_id: str) -> list[dict]:
    try:
        return _get(f"/debug/call/{call_id}").get("events", [])
    except Exception as e:
        print(f"    [timeline fetch failed: {e}]")
        return []


def _fetch_errors(call_id: str) -> list[dict]:
    timeline = _fetch_timeline(call_id)
    return [e for e in timeline if e.get("source") == "error"]


def _dialogue_state_summary(session_id: str) -> str:
    """Read the DialogueState via /debug/call by looking at state
    events.  Returns a compact per-task summary."""
    events = _fetch_timeline(session_id)
    tasks: dict[str, dict] = {}
    for e in reversed(events):   # replay oldest-first
        if e.get("source") != "state":
            continue
        p = e.get("payload") or {}
        if e.get("kind") == "task_added":
            tasks[p.get("task_id", "?")] = {
                "kind": p.get("kind"),
                "slots": {},
            }
        elif e.get("kind") == "slot_recorded":
            tid = p.get("task_id", "?")
            if tid not in tasks:
                tasks[tid] = {"kind": "?", "slots": {}}
            slot = p.get("slot")
            tasks[tid]["slots"][slot] = {
                "value": p.get("value"),
                "status": p.get("status"),
                "confidence": p.get("confidence"),
            }
    if not tasks:
        return "  (no state events)"
    lines = []
    for tid, t in tasks.items():
        lines.append(f"  {tid} ({t['kind']}):")
        for slot, sv in t["slots"].items():
            lines.append(
                f"    - {slot} = {sv['value']!r} [{sv['status']}, conf={sv['confidence']}]"
            )
    return "\n".join(lines)


def run() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print("VoiceOps intelligence test — 7-turn adversarial scenario")
    print("=" * 70)

    # Start
    start = _post("/chat/start", {})
    session_id = start["session_id"]
    greeting = start["greeting"]
    print(f"\nsession_id: {session_id}")
    print(f"greeting: {greeting}")

    greeting_path = OUT_DIR / "greeting.mp3"
    ok, ms = _synthesize_elevenlabs(greeting, greeting_path)
    print(f"greeting audio: {greeting_path if ok else 'SKIPPED'} ({ms:.0f}ms)")

    total_pass = 0
    total_fail = 0
    turn_start_all = time.monotonic()

    for step in SCRIPT:
        print("\n" + "-" * 70)
        print(f"[{step['id']}] probe: {step['probe']}")
        print(f"[{step['id']}] caller: {step['caller']!r}")

        started = time.monotonic()
        try:
            resp = _post(
                "/chat/turn",
                {"session_id": session_id, "text": step["caller"]},
                timeout=60.0,
            )
        except Exception as e:
            print(f"[{step['id']}] TURN FAILED: {e}")
            total_fail += 1
            continue
        latency_ms = (time.monotonic() - started) * 1000

        reply = resp.get("reply", "")
        print(f"[{step['id']}] agent ({latency_ms:.0f}ms): {reply!r}")

        # Audio
        audio_path = OUT_DIR / f"turn-{step['id']}.mp3"
        ok, tts_ms = _synthesize_elevenlabs(reply, audio_path)
        if ok:
            print(f"[{step['id']}] audio: {audio_path} ({tts_ms:.0f}ms)")

        # State summary
        print(f"[{step['id']}] dialogue state:")
        print(_dialogue_state_summary(session_id))

        # Tool results, errors
        tool_results = resp.get("tool_results", [])
        if tool_results:
            print(f"[{step['id']}] tools called:")
            for tr in tool_results:
                name = tr.get("name", "?")
                res = tr.get("result")
                err = tr.get("error")
                if err:
                    print(f"    - {name} ERROR: {err}")
                else:
                    print(f"    - {name} → {str(res)[:150]}")

        errors = _fetch_errors(session_id)
        if errors:
            recent = errors[:3]
            print(f"[{step['id']}] classified errors (recent {len(recent)}):")
            for er in recent:
                cat = er.get("error_category", "?")
                p = er.get("payload") or {}
                print(f"    - {cat}: {p.get('message', '?')[:120]}")

        print(f"[{step['id']}] expected hints (human check):")
        for h in step["expect_hints"]:
            print(f"    - {h}")

    total_ms = (time.monotonic() - turn_start_all) * 1000
    print("\n" + "=" * 70)
    print(f"TOTAL wall-clock: {total_ms:.0f}ms across {len(SCRIPT)} turns")
    print(f"audio files: {OUT_DIR}/")
    print(f"session id (for /debug/call): {session_id}")
    print("=" * 70)
    print()
    print("PLAY ORDER:")
    print(f"  1. greeting.mp3")
    for step in SCRIPT:
        print(f"  {step['id']}. turn-{step['id']}.mp3")
    print()
    print("HUMAN CHECK per turn:")
    print("  * did the reply text match the expected hint?")
    print("  * did the audio quality/pace/tone sound natural?")
    print("  * for T3: did the agent switch from Friday to Thursday?")
    print("  * for T4: did it answer insurance without losing the booking?")
    print("  * for T7: did it confirm with the committed date + name?")

    return 0


if __name__ == "__main__":
    sys.exit(run())

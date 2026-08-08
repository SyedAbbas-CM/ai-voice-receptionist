"""Outbound Twilio dial → your phone rings → intelligence stack answers.

Usage:
    python scripts/call_me.py                       # dials default PK number
    python scripts/call_me.py +447700900123         # dials override

Flow:
    Twilio REST API (from us) → dials your phone → you answer → Twilio
    fetches TwiML from TWILIO_PUBLIC_URL/twilio/voice → TwiML tells
    Twilio to open a Media Stream to /twilio/stream → our TwilioActorSession
    picks up → StreamingSTTBridge + TurnManager + full intelligence.

Cost: Twilio charges for the international outbound leg (PKR-to-US
Twilio number).  Recipient (you) pays nothing.  Verify Twilio balance
before running.
"""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

import httpx


def _load_env() -> None:
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        m = re.match(r"^([A-Z_][A-Z0-9_]*)=(.*)$", line.strip())
        if m and not line.strip().startswith("#"):
            os.environ.setdefault(m.group(1), m.group(2).strip())


_load_env()

TWILIO_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_FROM = os.environ["TWILIO_PHONE_NUMBER"]
PUBLIC_URL = os.environ.get("TWILIO_PUBLIC_URL", "").rstrip("/")
DEFAULT_TO = "+923318774222"   # PK cell (per prior sessions + CLAUDE.md)


def _preflight() -> None:
    print("═" * 60)
    print("  Pre-flight checks")
    print("═" * 60)

    # 1. Public URL set
    if not PUBLIC_URL:
        print("  ✗ TWILIO_PUBLIC_URL not set in .env")
        sys.exit(2)
    print(f"  ✓ public URL: {PUBLIC_URL}")

    # 2. Tunnel reachable
    try:
        r = httpx.get(f"{PUBLIC_URL}/health", timeout=5)
        r.raise_for_status()
        print(f"  ✓ tunnel healthy: {r.json()}")
    except Exception as e:
        print(f"  ✗ tunnel /health failed: {e}")
        print("    → is cloudflared running?")
        sys.exit(2)

    # 3. TwiML endpoint refuses unsigned requests — that's the guard
    # doing its job.  A real Twilio POST carries X-Twilio-Signature.
    try:
        r = httpx.post(f"{PUBLIC_URL}/twilio/voice", timeout=5)
        if r.status_code == 403 and "signature" in r.text.lower():
            print("  ✓ /twilio/voice guard active (rejects unsigned)")
        elif r.status_code == 401 and "signature" in r.text.lower():
            print("  ✓ /twilio/voice guard active (rejects unsigned)")
        elif "missing X-Twilio-Signature" in r.text:
            print("  ✓ /twilio/voice guard active (rejects unsigned)")
        elif r.status_code == 200 and "wss://" in r.text:
            print("  ✓ /twilio/voice returns TwiML (guard off for testing)")
        else:
            print(f"  ! /twilio/voice returned {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"  ✗ /twilio/voice: {e}")
        sys.exit(2)

    # 4. Intelligence flags on
    try:
        cfg = httpx.get(f"{PUBLIC_URL}/debug/config", timeout=5).json()
        flags = cfg.get("intelligence_flags", {})
        for k in ("twilio_use_actor", "dialogue_kernel_enabled",
                  "streaming_stt_enabled", "turn_manager_enabled",
                  "two_planner_enabled"):
            state = flags.get(k)
            print(f"  {'✓' if state else '✗'} {k} = {state}")
        print(f"  ✓ tts provider: {cfg.get('tts', {}).get('provider')}")
    except Exception as e:
        print(f"  ! /debug/config unavailable: {e}")

    # 5. Twilio account has balance (skip if API doesn't respond fast)
    try:
        r = httpx.get(
            f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Balance.json",
            auth=(TWILIO_SID, TWILIO_TOKEN), timeout=8,
        )
        if r.status_code == 200:
            bal = r.json()
            print(f"  ✓ Twilio balance: {bal.get('balance')} {bal.get('currency')}")
        else:
            print(f"  ! Twilio balance check {r.status_code}: {r.text[:100]}")
    except Exception as e:
        print(f"  ! Twilio balance skip: {e}")


def dial(to_number: str) -> None:
    print()
    print("═" * 60)
    print(f"  Placing call")
    print(f"  from: {TWILIO_FROM}")
    print(f"  to:   {to_number}")
    print(f"  webhook: {PUBLIC_URL}/twilio/voice")
    print("═" * 60)

    r = httpx.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Calls.json",
        auth=(TWILIO_SID, TWILIO_TOKEN),
        data={
            "From": TWILIO_FROM,
            "To": to_number,
            "Url": f"{PUBLIC_URL}/twilio/voice",
            "Method": "POST",
            # 30-second answer window; if you don't pick up, Twilio hangs up.
            "Timeout": "30",
        },
        timeout=30,
    )
    if r.status_code >= 400:
        print(f"  ✗ Twilio API {r.status_code}: {r.text[:400]}")
        sys.exit(3)
    call = r.json()
    print(f"  ✓ Call queued")
    print(f"    CallSid: {call.get('sid')}")
    print(f"    Status:  {call.get('status')}")
    print(f"    Uri:     {call.get('uri')}")
    print()
    print("  → your phone should ring in ~5-15 seconds")
    print("  → after the call, dump the timeline with:")
    print(f'      SID={call.get("sid")}')
    print(f'      curl -s {PUBLIC_URL}/debug/call/twilio_$SID/timeline | jq .')
    print()


if __name__ == "__main__":
    to = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TO
    _preflight()
    dial(to)

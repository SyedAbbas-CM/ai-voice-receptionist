"""Create or update a Vapi assistant that points at our custom-LLM webhook.

Usage:
    python scripts/create_vapi_assistant.py --create
    python scripts/create_vapi_assistant.py --update <assistant_id>

Env vars needed:
    VAPI_PRIVATE_KEY   - from https://dashboard.vapi.ai (API keys tab)
    VAPI_PUBLIC_URL    - your backend URL, e.g. https://<subdomain>.trycloudflare.com
    VAPI_SECRET        - shared secret we check on the webhook (optional)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from packages.schemas import BusinessProfile  # noqa: E402
from packages.core_agent import build_system_prompt  # noqa: E402


VAPI_API = "https://api.vapi.ai"


def build_assistant_config(business: BusinessProfile, public_url: str, secret: str | None) -> dict:
    system_prompt = build_system_prompt(business)

    headers = {}
    if secret:
        headers["Authorization"] = f"Bearer {secret}"

    return {
        "name": f"{business.name} receptionist",
        "firstMessage": f"Hi, thanks for calling {business.name}. How can I help you today?",
        "model": {
            "provider": "custom-llm",
            "url": f"{public_url.rstrip('/')}/vapi/chat",
            "model": "voiceops-brain",
            "temperature": 0.3,
            "messages": [{"role": "system", "content": system_prompt}],
            **({"headers": headers} if headers else {}),
        },
        "voice": {
            "provider": "11labs",
            "voiceId": os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM"),
            "model": os.environ.get("ELEVENLABS_MODEL", "eleven_turbo_v2_5"),
        },
        "transcriber": {
            "provider": "deepgram",
            "model": os.environ.get("DEEPGRAM_MODEL", "nova-3"),
            "language": "en",
        },
        "serverUrl": f"{public_url.rstrip('/')}/vapi/events",
        "serverUrlSecret": secret or "",
        "endCallFunctionEnabled": True,
        "recordingEnabled": True,
        "silenceTimeoutSeconds": 20,
        "maxDurationSeconds": 900,
    }


def load_business() -> BusinessProfile:
    path = os.environ.get("BUSINESS_PROFILE_PATH") or str(REPO_ROOT / "sample-data" / "clinic" / "business.json")
    return BusinessProfile(**json.loads(Path(path).read_text()))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--create", action="store_true", help="Create a new assistant")
    ap.add_argument("--update", metavar="ASSISTANT_ID", help="Update an existing assistant by id")
    args = ap.parse_args()

    if not (args.create or args.update):
        ap.error("pass either --create or --update <assistant_id>")

    api_key = os.environ.get("VAPI_PRIVATE_KEY")
    public_url = os.environ.get("VAPI_PUBLIC_URL")
    secret = os.environ.get("VAPI_SECRET") or None

    if not api_key or not public_url:
        print("ERROR: set VAPI_PRIVATE_KEY and VAPI_PUBLIC_URL", file=sys.stderr)
        return 2

    business = load_business()
    config = build_assistant_config(business, public_url, secret)

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    with httpx.Client(timeout=30) as client:
        if args.create:
            r = client.post(f"{VAPI_API}/assistant", headers=headers, json=config)
        else:
            r = client.patch(f"{VAPI_API}/assistant/{args.update}", headers=headers, json=config)

    if r.status_code >= 400:
        print(f"Vapi API {r.status_code}: {r.text}", file=sys.stderr)
        return 1

    data = r.json()
    print(json.dumps(data, indent=2))
    print(f"\n✓ Assistant id: {data.get('id')}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

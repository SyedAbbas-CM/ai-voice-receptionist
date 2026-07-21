"""Local Vapi emulator.

Mimics Vapi's role from the outbound-dialer's perspective:

  - Same dispatch shape: dispatch_call(assistant_id, phone_number_id,
    customer_number, variable_values) -> (call_id, "queued")
  - Runs a full inbound-style conversation locally:
      1. builds the assistant's system prompt from BusinessProfile
      2. LLM generates the opening line (uses variable_values)
      3. loops: LLM -> Qwen3-TTS clone -> saves audio -> simulates caller
         response -> LLM -> ...  (script-driven for the demo path)
      4. detects capture_disposition tool call and ends
  - Fires an end-of-call event to /vapi/events so the same disposition
    handler + Google Sheets writeback still runs

For a real production build we would replace step (3) with a Twilio Media
Stream: our audio goes out over PSTN, the caller's audio comes back, STT
transcribes it. That's a separate wiring, not part of the emulator.

This module lets you demo the entire product with:
  - no Vapi account
  - no Twilio account
  - no phone number
  - just Qwen3-TTS on your machine

The "call" produces a transcript file + audio files under
  data/local_calls/<call_id>/
which the user can play back like a real recording.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx

from app.core.config import settings
from app.providers import get_llm, get_tts
from packages.core_agent.brain import ReceptionistBrain
from packages.core_agent.prompt import build_system_prompt
from packages.integrations import build_tools_for_vertical
from packages.integrations.fake_calendar import FakeCalendar
from packages.schemas import BusinessProfile, CallState, TranscriptTurn, TurnRole


log = logging.getLogger(__name__)


@dataclass
class LocalDispatchResult:
    """Same public shape as VapiClient.DispatchResult so callers are agnostic."""

    id: str
    status: str
    raw: dict = field(default_factory=dict)


# ---------- deterministic caller for demo mode ----------

DEFAULT_CALLER_SCRIPT = [
    "Uh, sure. What's this about?",
    "Yeah it's still available. Rent's actually about seventeen fifty now, not fifteen.",
    "Hmm, seller financing. Interesting. Can you tell me more?",
    "Send me the details. Call me back tomorrow afternoon.",
]

HOSTILE_CALLER_SCRIPT = [
    "Who is this?",
    "Take me off your list. I'm not interested.",
]


class LocalVoiceOrchestrator:
    """The emulator itself. One instance is enough — it's stateless."""

    def __init__(
        self,
        business: BusinessProfile,
        caller_script: Optional[list[str]] = None,
        output_dir: Optional[Path] = None,
        events_webhook_url: Optional[str] = None,
    ) -> None:
        self.business = business
        self.caller_script = caller_script or DEFAULT_CALLER_SCRIPT
        self.output_dir = output_dir or (Path(settings.calendar_path).parent / "local_calls")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        # Where to POST end-of-call events so the disposition handler runs.
        # Defaults to the same server, which is fine when both live in one uvicorn.
        self.events_webhook_url = events_webhook_url or (
            (settings.vapi_public_url or "http://localhost:8000").rstrip("/") + "/vapi/events"
        )

    async def dispatch_call(
        self,
        assistant_id: str,
        phone_number_id: str,
        customer_number: str,
        variable_values: Optional[dict] = None,
    ) -> LocalDispatchResult:
        """Fire a simulated call. Same signature as VapiClient.dispatch_call.

        Returns immediately with a synthetic call_id; the actual "call" runs
        in the background and posts to the events webhook when done."""
        call_id = f"local_{uuid.uuid4().hex[:12]}"
        asyncio.create_task(self._run_call(call_id, customer_number, variable_values or {}))
        return LocalDispatchResult(
            id=call_id,
            status="queued",
            raw={"assistant_id": assistant_id, "phone_number_id": phone_number_id},
        )

    async def _run_call(self, call_id: str, customer_number: str, variables: dict) -> None:
        """Run one simulated call end-to-end."""
        call_dir = self.output_dir / call_id
        call_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = call_dir / "transcript.jsonl"
        transcript_lines: list[str] = []
        transcript_summary: list[str] = []

        try:
            state = CallState(session_id=f"vapi_{call_id}", business_id=self.business.id)
            llm = get_llm()

            # Same tool factory the receptionist uses. For wholesaler_outbound
            # the brain gets capture_disposition + record_rent_update tools.
            calendar = FakeCalendar(settings.calendar_path)
            tools, tool_handler = build_tools_for_vertical(self.business, calendar)

            # Inject variables into the system prompt so {{lead_name}}, {{property_address}},
            # {{rent_amount}} placeholders resolve at call start — mirrors what Vapi does with
            # assistantOverrides.variableValues.
            base_prompt = build_system_prompt(self.business)
            resolved_prompt = _resolve_variables(base_prompt, variables)
            if variables:
                resolved_prompt += "\n\nCALL CONTEXT:\n" + "\n".join(
                    f"- {k}: {v}" for k, v in variables.items()
                )

            brain = ReceptionistBrain(
                llm=llm,
                business=self.business,
                tools=tools,
                tool_handler=tool_handler,
            )
            brain.system_prompt = resolved_prompt

            # Opening greeting
            greeting = await brain.greet(state)
            await self._speak_and_log(greeting.reply, call_dir, transcript_lines,
                                       transcript_summary, role="assistant", turn=0)

            # Drive the "conversation" via the caller script
            for turn_i, caller_line in enumerate(self.caller_script, start=1):
                transcript_lines.append(json.dumps({
                    "turn": turn_i, "role": "user", "text": caller_line,
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                }))
                transcript_summary.append(f"USER: {caller_line}")

                result = await brain.handle_user_turn(state, caller_line)
                await self._speak_and_log(result.reply, call_dir, transcript_lines,
                                           transcript_summary, role="assistant", turn=turn_i)

                # Break if the brain called capture_disposition
                captured = getattr(tool_handler, "captured_disposition", None)
                if captured:
                    transcript_lines.append(json.dumps({
                        "role": "tool", "tool": "capture_disposition", "result": captured,
                    }))
                    break
                if result.escalated:
                    break

            transcript_path.write_text("\n".join(transcript_lines) + "\n")
            log.info("local call %s finished; transcript at %s", call_id, transcript_path)

            # Post end-of-call event to /vapi/events so the disposition handler runs
            captured = getattr(tool_handler, "captured_disposition", None)
            rent_update = getattr(tool_handler, "rent_update", None)
            ended_reason = "hangup"
            if captured and captured.get("disposition") == "NO_ANSWER":
                ended_reason = "no-answer"

            await self._post_end_of_call(
                call_id=call_id,
                customer_number=customer_number,
                transcript_summary="\n".join(transcript_summary),
                ended_reason=ended_reason,
                captured_disposition=captured,
                rent_update=rent_update,
            )
        except Exception as e:
            log.exception("local call %s failed: %s", call_id, e)
            transcript_lines.append(json.dumps({"error": str(e)}))
            transcript_path.write_text("\n".join(transcript_lines) + "\n")

    async def _speak_and_log(
        self,
        text: str,
        call_dir: Path,
        transcript_lines: list[str],
        transcript_summary: list[str],
        role: str,
        turn: int,
    ) -> None:
        transcript_lines.append(json.dumps({
            "turn": turn, "role": role, "text": text,
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }))
        transcript_summary.append(f"{role.upper()}: {text}")

        tts = get_tts()
        try:
            audio_bytes, mime = await tts.synthesize(text)
        except Exception as e:
            log.warning("TTS failed for turn %s: %s", turn, e)
            return
        if mime == "text/x-browser-speak" or not audio_bytes:
            return
        ext = "wav" if "wav" in mime else "mp3"
        (call_dir / f"turn_{turn:03d}_{role}.{ext}").write_bytes(audio_bytes)

    async def _post_end_of_call(
        self,
        call_id: str,
        customer_number: str,
        transcript_summary: str,
        ended_reason: str,
        captured_disposition: Optional[dict],
        rent_update: Optional[dict],
    ) -> None:
        """POST an end-of-call-report event mimicking Vapi's shape."""
        event = {
            "message": {
                "type": "end-of-call-report",
                "call": {"id": call_id, "customer": {"number": customer_number}},
                "transcript": transcript_summary,
                "endedReason": ended_reason,
                "artifact": {"transcript": transcript_summary},
                # Extra: pass the tool-captured signals so the disposition handler
                # can use them if the LLM classification is uncertain
                "local_meta": {
                    "captured_disposition": captured_disposition,
                    "rent_update": rent_update,
                },
            }
        }
        try:
            headers = {}
            if settings.vapi_secret:
                headers["Authorization"] = f"Bearer {settings.vapi_secret}"
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.post(self.events_webhook_url, json=event, headers=headers)
            if r.status_code >= 400:
                log.warning("end-of-call POST failed %s: %s", r.status_code, r.text[:200])
        except Exception as e:
            log.warning("could not post end-of-call event: %s", e)


def _resolve_variables(template: str, variables: dict) -> str:
    """Replace {{ var }} placeholders in a system prompt with actual values.
    Matches the shape Vapi's assistantOverrides.variableValues does server-side."""
    out = template
    for key, val in (variables or {}).items():
        out = out.replace(f"{{{{ {key} }}}}", str(val))
        out = out.replace(f"{{{{{key}}}}}", str(val))
        out = out.replace(f"{{{{ {key}}}}}", str(val))
        out = out.replace(f"{{{{{key} }}}}", str(val))
    return out

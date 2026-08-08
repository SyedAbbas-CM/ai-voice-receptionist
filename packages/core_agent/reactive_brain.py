"""Task B: Reactive+Committed brain (shadow build, feature-flagged OFF).

Instead of `user_text → reply → TTS`, this brain returns structured
JSON with three possible lanes:

  lane = "silent"      → understand the turn, update state, do NOT speak
  lane = "backchannel" → play a short cached ack ("mm-hm", "one sec"), cheap
  lane = "commit"      → full committed reply, spoken via ElevenLabs

Model output schema:
    {
      "should_speak": bool,
      "backchannel": str | null,       # from a fixed vocabulary
      "internal_thoughts": str,        # what the model understood so far
      "state_updates": dict,           # kernel hints (optional)
      "committed_reply": str | null    # only when should_speak=true and not a backchannel
    }

The reactive brain is completely SEPARATE from the current
ReceptionistBrain — it's a shadow build.  Enable via
REACTIVE_BRAIN_ENABLED=true.  Off = current brain path, zero
behavior change.

Observability:
    voiceops_reactive_brain_lane_total{tenant_id, lane}
    voiceops_reactive_brain_latency_seconds{tenant_id, lane}
Structured JSON logs emitted on every lane decision so we can diff
committed-brain vs reactive-brain outputs side by side.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from packages.runtime import telemetry as _tel


log = logging.getLogger(__name__)


# Backchannels we PRE-CACHE via Task A warmup.  Reactive brain must
# only choose from this set — otherwise a cache miss on the "cheap"
# lane costs an ElevenLabs call and defeats the point.
ALLOWED_BACKCHANNELS: tuple[str, ...] = (
    "Mm-hmm.",
    "Okay.",
    "Sure.",
    "Got it.",
    "Right.",
    "Yeah.",
    "Alright.",
    "One second.",
    "Let me see.",
    "Hmm.",
    "No problem.",
    "Perfect.",
    "Absolutely.",
    "Of course.",
    "Sounds good.",
)


REACTIVE_SYSTEM_ADDITION = """\
YOU ARE OPERATING IN REACTIVE MODE.

Every turn you MUST reply with a single JSON object matching this schema:
{
  "should_speak": true|false,
  "backchannel": null | one of: %s,
  "internal_thoughts": "1-2 sentences on what the caller wants so far",
  "state_updates": {},
  "committed_reply": null | "<full spoken reply>"
}

Rules:
- Choose lane based on caller's turn:
  * "silent": caller is mid-thought, ambiguous, or gave a partial fragment
      → should_speak=false, backchannel=null, committed_reply=null
      → write what you understood in internal_thoughts
  * "backchannel": caller made a small point that deserves acknowledgement
      but the full turn isn't ready to answer yet (e.g. paused after
      providing a fragment, or a moment of thinking on their side)
      → should_speak=true, backchannel="Mm-hmm." (choose from the list),
         committed_reply=null
  * "commit": caller finished a clear turn deserving a full spoken reply
      → should_speak=true, backchannel=null, committed_reply="..."

- NEVER emit both backchannel and committed_reply.
- Backchannel text MUST come from the allowed list, verbatim.
- Keep committed_reply to 1-2 sentences unless caller explicitly asks for detail.
- ALL other persona rules (identity lock, business scope, tools) still apply.

Return ONLY the JSON object, no prose before or after.
""" % (", ".join(f'"{b}"' for b in ALLOWED_BACKCHANNELS))


@dataclass
class ReactiveReply:
    """Parsed brain output.  Only one of backchannel/committed_reply
    will be set when should_speak=true."""
    should_speak: bool
    backchannel: Optional[str]
    committed_reply: Optional[str]
    internal_thoughts: str
    state_updates: dict = field(default_factory=dict)
    raw_json: str = ""

    @property
    def lane(self) -> str:
        if not self.should_speak:
            return "silent"
        if self.backchannel:
            return "backchannel"
        if self.committed_reply:
            return "commit"
        return "error"


# ── parsing ────────────────────────────────────────────────────────


_JSON_FENCE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL | re.IGNORECASE)


def parse_reactive_reply(raw: str) -> ReactiveReply:
    """Robust parse.  Handles common LLM mistakes: fenced code blocks,
    leading prose, trailing whitespace.  Falls back to a safe COMMIT
    lane echoing the raw text on parse failure."""
    if not raw or not raw.strip():
        return _fallback_error("empty output", raw)

    candidate = raw.strip()
    # Strip markdown fences.
    m = _JSON_FENCE.search(candidate)
    if m:
        candidate = m.group(1).strip()
    # Find first '{' to last '}' — models sometimes prepend "Here is my reply:"
    lb = candidate.find("{")
    rb = candidate.rfind("}")
    if lb == -1 or rb == -1 or rb <= lb:
        return _fallback_error("no JSON object found", raw)
    candidate = candidate[lb : rb + 1]

    try:
        obj = json.loads(candidate)
    except json.JSONDecodeError as e:
        return _fallback_error(f"json decode: {e}", raw)

    if not isinstance(obj, dict):
        return _fallback_error("json not object", raw)

    should_speak = bool(obj.get("should_speak", False))
    backchannel = obj.get("backchannel") or None
    committed = obj.get("committed_reply") or None
    thoughts = str(obj.get("internal_thoughts", ""))
    state_updates = obj.get("state_updates") or {}
    if not isinstance(state_updates, dict):
        state_updates = {}

    # Validate backchannel is from the allowed list — if the LLM
    # invented one, coerce to closest match or drop.
    if backchannel and backchannel not in ALLOWED_BACKCHANNELS:
        matched = _coerce_backchannel(backchannel)
        if matched is not None:
            backchannel = matched
        else:
            log.warning("reactive_brain: dropping unknown backchannel %r", backchannel)
            backchannel = None
            # If they wanted to speak but bc was bogus, either give a
            # neutral ack or force silent.  Prefer silent (safer, no
            # bogus audio).
            if not committed:
                should_speak = False

    # Sanity: don't allow both backchannel + committed_reply.
    if backchannel and committed:
        log.warning("reactive_brain: both bc + commit set; preferring commit")
        backchannel = None

    return ReactiveReply(
        should_speak=should_speak,
        backchannel=backchannel,
        committed_reply=committed,
        internal_thoughts=thoughts,
        state_updates=state_updates,
        raw_json=candidate,
    )


def _coerce_backchannel(bc: str) -> Optional[str]:
    """Try to snap a model-invented backchannel to an allowed one."""
    n = bc.strip().lower().rstrip(".!?,")
    for allowed in ALLOWED_BACKCHANNELS:
        if allowed.lower().rstrip(".!?,") == n:
            return allowed
    return None


def _fallback_error(reason: str, raw: str) -> ReactiveReply:
    """If parsing fails, treat the whole raw as a commit reply so the
    caller still hears something.  Log loudly."""
    log.warning("reactive_brain parse fallback (%s): raw=%r", reason, raw[:200])
    return ReactiveReply(
        should_speak=True,
        backchannel=None,
        committed_reply=raw.strip()[:400] or "Sorry, I lost you for a moment.",
        internal_thoughts=f"[parse error: {reason}]",
        state_updates={},
        raw_json=raw,
    )


# ── running-notes notepad ─────────────────────────────────────────


def build_notepad_message(
    notes: list[str],
    max_entries: int = 5,
) -> Optional[dict]:
    """Compact the running internal_thoughts into a system message
    for the next LLM call.  Cap at max_entries to bound context
    growth.  Returns None if nothing to say."""
    if not notes:
        return None
    tail = notes[-max_entries:]
    body = "\n".join(f"- {n}" for n in tail if n)
    if not body.strip():
        return None
    return {
        "role": "system",
        "content": (
            "RUNNING NOTES (your prior internal thoughts on this call, "
            "for continuity):\n" + body
        ),
    }


# ── dispatcher ────────────────────────────────────────────────────


async def reactive_turn(
    llm_provider,
    system_prompt: str,
    transcript_messages: list[dict],
    running_notes: list[str],
    tools: Optional[list] = None,
    tenant_id: str = "default",
    temperature: float = 0.2,
) -> ReactiveReply:
    """Run one reactive-brain turn.

    Wraps the existing LLM provider — the caller passes the same
    provider used by the committed brain (routes through the router
    with all its fallbacks).  Only the prompt shape changes.
    """
    messages = [
        {"role": "system", "content": system_prompt + "\n\n" + REACTIVE_SYSTEM_ADDITION},
    ]
    note_msg = build_notepad_message(running_notes)
    if note_msg is not None:
        messages.append(note_msg)
    messages.extend(transcript_messages)

    t0 = time.perf_counter()
    lane_label = "error"
    try:
        # Force JSON when provider supports it.  Some providers (Gemini)
        # ignore the kwarg, which is fine — parse_reactive_reply handles
        # loose text too.
        resp = await llm_provider.complete(
            messages,
            tools=tools,
            temperature=temperature,
            max_tokens=400,
        )
        reply = parse_reactive_reply(resp.text or "")
        lane_label = reply.lane
        return reply
    finally:
        _tel.REACTIVE_BRAIN_LANE.labels(tenant_id=tenant_id, lane=lane_label).inc()
        _tel.REACTIVE_BRAIN_LATENCY.labels(
            tenant_id=tenant_id, lane=lane_label,
        ).observe(time.perf_counter() - t0)
        log.info(
            "reactive_brain call: lane=%s latency=%.0fms tenant=%s",
            lane_label, 1000 * (time.perf_counter() - t0), tenant_id,
        )

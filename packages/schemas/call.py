from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

from .tools import ToolCall


class TurnRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    SYSTEM = "system"


class CallStatus(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    ESCALATED = "escalated"
    ABANDONED = "abandoned"


class Intent(str, Enum):
    BOOK_APPOINTMENT = "book_appointment"
    RESCHEDULE = "reschedule"
    CANCEL = "cancel"
    FAQ = "faq"
    EMERGENCY = "emergency"
    HANDOFF = "handoff"
    OTHER = "other"


class Urgency(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Sentiment(str, Enum):
    """Rolling caller sentiment. Updated per turn by extractor.

    An angry caller is invisible until end-of-call otherwise — this lets
    the dashboard / disposition layer flag warnings ("caller getting
    frustrated") before the call ends."""
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"
    FRUSTRATED = "frustrated"


class TranscriptTurn(BaseModel):
    """One turn in a call.

    Roles map to LLM message roles per OpenAI/Groq spec:
      - USER      → {"role": "user", "content": text}
      - ASSISTANT → {"role": "assistant", "content": text}
                    or with `tool_calls` populated: full assistant tool-call message
      - TOOL      → {"role": "tool", "tool_call_id": ..., "content": ...}
                    tool_call_id MUST match one from a prior assistant turn
      - SYSTEM    → {"role": "system", "content": text}

    Tool-call bookkeeping (required by the OpenAI/Groq function-calling spec —
    without these fields, follow-up LLM calls after a tool dispatch 400):
      - tool_calls: on an assistant turn, list of ToolCall the LLM asked us to run
      - tool_call_id: on a TOOL turn, binds this result back to the assistant's
                       tool_calls[i].id — Groq validates the pairing strictly.
    """
    role: TurnRole
    text: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    # Assistant tool-call emission
    tool_calls: Optional[list[ToolCall]] = None
    # Tool-result bookkeeping (on role=TOOL turns)
    tool_call_id: Optional[str] = None
    tool_name: Optional[str] = None
    tool_args: Optional[dict] = None
    tool_result: Optional[dict] = None
    # Phase 2 (2026-08-30, task #95): scope-change instructions + tool
    # error. LK judges use `agent_instructions_delta` to grade turns
    # under the effective instructions at that moment (sub-agent scope
    # enter/exit populates this). `tool_error` disambiguates a genuine
    # null result from a tool that raised. Both nullable; session
    # persist reads via getattr so pre-existing Turn objects still work.
    agent_instructions_delta: Optional[str] = None
    tool_error: Optional[str] = None


class ExtractedFields(BaseModel):
    caller_name: Optional[str] = None
    phone: Optional[str] = None
    intent: Intent = Intent.OTHER
    service: Optional[str] = None
    preferred_date: Optional[str] = None
    preferred_time: Optional[str] = None
    urgency: Urgency = Urgency.LOW
    lead_score: int = 0
    summary: str = ""
    # Rolling caller sentiment. Refreshed on every extractor pass so a
    # dashboard can raise an alert as soon as it turns FRUSTRATED without
    # waiting for end-of-call.
    sentiment: Sentiment = Sentiment.NEUTRAL

    def missing_required_for_booking(self) -> list[str]:
        required = ["caller_name", "phone", "service", "preferred_date", "preferred_time"]
        return [f for f in required if not getattr(self, f)]

    def is_frustrated(self) -> bool:
        return self.sentiment in (Sentiment.NEGATIVE, Sentiment.FRUSTRATED)


class CallState(BaseModel):
    session_id: str
    business_id: str
    # RE-AUDIT FIX 2026-08-02 (CRITICAL-01): tenant ownership on every
    # active call.  session_manager rejects lookups where the caller's
    # tenant_id doesn't match this value.  Defaults to "default" for
    # backward compat with single-tenant deploys where auth is disabled.
    tenant_id: str = "default"
    status: CallStatus = CallStatus.ACTIVE
    started_at: datetime = Field(default_factory=datetime.utcnow)
    ended_at: Optional[datetime] = None
    transcript: list[TranscriptTurn] = Field(default_factory=list)
    extracted: ExtractedFields = Field(default_factory=ExtractedFields)
    escalation_reason: Optional[str] = None
    # Sprint 10 WIRING (2026-08-04): dialogue kernel state lives
    # alongside the legacy fields during migration.  Optional so pre-
    # kernel callers still work.  When the kernel is enabled, the
    # brain writes StatePatches; consumers migrate to reading from
    # here one at a time.  Type is `dict` in the schema so we don't
    # circular-import packages/dialogue; runtime type is DialogueState.
    dialogue: Optional[dict] = None

    # K3+K4 (2026-08-05): last classified turn intent.  Populated by
    # brain.handle_user_turn from classifiers/turn_intent.py.  Not
    # persisted to LLM history — used inline for the next system-note
    # injection, then overwritten on the following turn.
    # Typed as `object` so pydantic doesn't try to serialize the enum.
    last_turn_intent: Optional[object] = None

    model_config = {"arbitrary_types_allowed": True}

    def add_turn(self, turn: TranscriptTurn) -> None:
        self.transcript.append(turn)

    def to_llm_messages(self) -> list[dict]:
        """Serialize the transcript to OpenAI/Groq chat-completion message format.

        SPEC:
          user       -> {"role": "user", "content": text}
          assistant  -> {"role": "assistant", "content": text or ""}
                        + if tool_calls: {"tool_calls": [{"id","type","function":{"name","arguments"}}]}
          tool       -> {"role": "tool", "tool_call_id": <id>, "content": stringified_result}
                        MUST directly follow an assistant turn that emitted a tool_call
                        with matching id, or Groq returns 400.
          system     -> {"role": "system", "content": text}

        Consecutive assistant turns without tool_calls are safe; consecutive
        TOOL turns are safe (multi-tool assistant call).
        """
        messages: list[dict] = []
        for turn in self.transcript:
            if turn.role == TurnRole.TOOL:
                # Content is a JSON string per OpenAI spec — models parse it more
                # reliably than a repr() dump. Fall back to str() if not JSON-safe.
                payload = turn.tool_result if turn.tool_result is not None else turn.text
                try:
                    content_str = json.dumps(payload, default=str)
                except (TypeError, ValueError):
                    content_str = str(payload)
                messages.append({
                    "role": "tool",
                    "tool_call_id": turn.tool_call_id or "",
                    "content": content_str,
                })
            elif turn.role == TurnRole.ASSISTANT:
                msg: dict[str, Any] = {"role": "assistant", "content": turn.text or ""}
                if turn.tool_calls:
                    msg["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                # OpenAI/Groq expect arguments as a JSON string, not object
                                "arguments": json.dumps(tc.arguments, default=str),
                            },
                        }
                        for tc in turn.tool_calls
                    ]
                    # Content SHOULD be either the text the model streamed alongside
                    # the tool call, or an empty string (never None) when only tools
                    # were emitted.
                    if not msg["content"]:
                        msg["content"] = ""
                messages.append(msg)
            else:
                messages.append({"role": turn.role.value, "content": turn.text})
        return messages

from __future__ import annotations

import time
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.core import session_manager
from app.core.config import settings


router = APIRouter(prefix="/vapi", tags=["vapi"])


class ChatMessage(BaseModel):
    role: str
    content: str | None = None
    name: str | None = None
    tool_call_id: str | None = None


class VapiCompletionRequest(BaseModel):
    """Vapi custom-LLM calls us with an OpenAI-compatible payload.

    We ignore most fields and only look at `messages` and the `call` metadata
    Vapi appends. Our brain owns the actual turn logic — we just return the
    reply in OpenAI shape."""

    model: str | None = None
    messages: list[ChatMessage]
    temperature: float | None = 0.3
    stream: bool | None = False
    call: dict | None = None
    metadata: dict | None = None


def _last_user_text(messages: list[ChatMessage]) -> str:
    for msg in reversed(messages):
        if msg.role == "user" and msg.content:
            return msg.content
    return ""


def _session_id_from_request(req: VapiCompletionRequest) -> str:
    if req.call and req.call.get("id"):
        return f"vapi_{req.call['id']}"
    if req.metadata and req.metadata.get("session_id"):
        return str(req.metadata["session_id"])
    return f"vapi_{uuid.uuid4().hex[:12]}"


def _openai_response(reply: str, model: str) -> dict:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:16]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model or "voiceops-brain",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": reply},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


@router.post("/chat/completions")
async def vapi_chat_completions(req: VapiCompletionRequest, request: Request) -> dict:
    """Vapi's custom-LLM endpoint. Vapi handles STT/TTS/telephony; we own the brain."""
    if settings.vapi_secret:
        auth = request.headers.get("authorization", "")
        if not auth.endswith(settings.vapi_secret):
            raise HTTPException(status_code=401, detail="invalid webhook secret")

    session_id = _session_id_from_request(req)
    handle = session_manager.get_session(session_id)
    if handle is None:
        state, brain = session_manager.start_session_with_id(session_id)
        await session_manager.run_greeting(state, brain)
    else:
        state, brain = handle

    user_text = _last_user_text(req.messages)
    if not user_text:
        return _openai_response(f"Hi, thanks for calling {brain.business.name}. How can I help you today?", req.model)

    payload = await session_manager.run_user_turn(state, brain, user_text)
    return _openai_response(payload["reply"], req.model)


class VapiEventPayload(BaseModel):
    message: dict


@router.post("/events")
async def vapi_events(payload: VapiEventPayload, request: Request) -> dict:
    """Vapi call lifecycle events: end-of-call, hang-up, status changes.
    We use end-of-call-report to close the session and finalize DB rows."""
    if settings.vapi_secret:
        auth = request.headers.get("authorization", "")
        if not auth.endswith(settings.vapi_secret):
            raise HTTPException(status_code=401, detail="invalid webhook secret")

    msg = payload.message or {}
    msg_type = msg.get("type")
    call = msg.get("call") or {}
    call_id = call.get("id")

    if msg_type == "end-of-call-report" and call_id:
        # Outbound disposition (SubtoDealz-style) — checks its own registry.
        # If this call was originated via /outbound/start_batch, the handler
        # runs the transcript extractor + lead classifier and writes back to
        # the source sheet. Otherwise it's a no-op.
        try:
            from app.core.disposition_handler import process_end_of_call
            outbound_outcome = await process_end_of_call(msg)
        except Exception:
            outbound_outcome = None

        await session_manager.end_session_async(f"vapi_{call_id}")
        if outbound_outcome and outbound_outcome.get("reason") != "not_outbound":
            return {"ok": True, "outbound": outbound_outcome}

    return {"ok": True}

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
async def vapi_chat_completions(request: Request) -> dict:
    """Vapi's custom-LLM endpoint. Vapi handles STT/TTS/telephony; we own the brain.

    Accepts the raw request body so we can log + tolerate Vapi's payload-shape
    drift.  2026-07-31: users reported the agent going silent mid-call; server
    logs showed the POST landed with 200 OK but no LLM span fired for the turn.
    Root-cause turned out to be Pydantic silently accepting a payload where
    `messages` was empty (Vapi's newer shape wraps under `message.artifact` or
    similar).  Now we parse defensively and log every payload for debugging.
    """
    import logging
    _log = logging.getLogger(__name__)

    # AUDIT FIX 2026-08-01 (WH-005): mandatory in prod, constant-time compare
    import os as _os, hmac as _hmac
    _enforce = _os.environ.get("VAPI_SIGNATURE_ENFORCE", "true").lower() not in ("0", "false", "no")
    if _enforce:
        if not settings.vapi_secret:
            raise HTTPException(status_code=500, detail="VAPI_SECRET not configured")
        auth = request.headers.get("authorization", "")
        # Accept both "Bearer <secret>" and bare "<secret>" forms
        provided = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else auth.strip()
        if not _hmac.compare_digest(provided, settings.vapi_secret):
            raise HTTPException(status_code=401, detail="invalid webhook secret")

    body = await request.json()
    _log.info("VAPI POST body keys=%s", list(body.keys())[:20])

    # Extract messages from any of Vapi's known payload shapes
    messages = body.get("messages")
    call_meta = body.get("call") or {}

    # New Vapi wrapper shape: {"message": {"artifact": {"messages": [...]}, "call": {...}}}
    if not messages and isinstance(body.get("message"), dict):
        wrapped = body["message"]
        artifact = wrapped.get("artifact") or {}
        messages = artifact.get("messages") or wrapped.get("messages")
        call_meta = wrapped.get("call") or call_meta

    if not messages:
        _log.warning("VAPI: no messages found in payload. Body keys=%s", list(body.keys()))
        # Return greeting instead of silence
        try:
            biz_name = session_manager.load_business().name
        except Exception:
            biz_name = "our office"
        return _openai_response(
            f"Hi, thanks for calling {biz_name}. How can I help you today?",
            body.get("model") or "voiceops-brain",
        )

    # Coerce messages into simple {role, content} dicts
    coerced_messages = []
    for m in messages:
        if isinstance(m, dict):
            coerced_messages.append({
                "role": m.get("role", "user"),
                "content": m.get("content") or m.get("message") or "",
            })

    _log.info(
        "VAPI turn: session=%s, msg_count=%d, last_user=%r",
        call_meta.get("id", "?")[:12],
        len(coerced_messages),
        (coerced_messages[-1]["content"] if coerced_messages else "")[:80],
    )

    session_id = f"vapi_{call_meta.get('id', uuid.uuid4().hex[:12])}"
    # RE-AUDIT 2026-08-02 (CRITICAL-01/12): tenant on vapi sessions is "default"
    # until CRITICAL-12 (tenant resolver from provider identifiers) lands in
    # Sprint 7 later this week.  Interim state = single-tenant Vapi deploys
    # keep working; multi-tenant Vapi deploys pending resolver.
    tenant_id = "default"
    handle = session_manager.get_session(session_id, tenant_id=tenant_id)
    if handle is None:
        state, brain = session_manager.start_session_with_id(session_id, tenant_id=tenant_id)
        await session_manager.run_greeting(state, brain)
    else:
        state, brain = handle

    # Find the last user turn
    user_text = ""
    for msg in reversed(coerced_messages):
        if msg["role"] == "user" and msg["content"]:
            user_text = msg["content"]
            break

    if not user_text:
        return _openai_response(
            f"Hi, thanks for calling {brain.business.name}. How can I help you today?",
            body.get("model"),
        )

    _log.info("VAPI running brain turn: %r", user_text[:80])
    payload = await session_manager.run_user_turn(state, brain, user_text)
    reply = payload.get("reply", "I didn't catch that, can you say it again?")
    _log.info("VAPI reply (%d chars): %r", len(reply), reply[:100])
    return _openai_response(reply, body.get("model"))


class VapiEventPayload(BaseModel):
    message: dict


@router.post("/events")
async def vapi_events(payload: VapiEventPayload, request: Request) -> dict:
    """Vapi call lifecycle events: end-of-call, hang-up, status changes.
    We use end-of-call-report to close the session and finalize DB rows."""
    # AUDIT FIX 2026-08-01 (WH-005): mandatory in prod, constant-time compare
    import os as _os, hmac as _hmac
    _enforce = _os.environ.get("VAPI_SIGNATURE_ENFORCE", "true").lower() not in ("0", "false", "no")
    if _enforce:
        if not settings.vapi_secret:
            raise HTTPException(status_code=500, detail="VAPI_SECRET not configured")
        auth = request.headers.get("authorization", "")
        # Accept both "Bearer <secret>" and bare "<secret>" forms
        provided = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else auth.strip()
        if not _hmac.compare_digest(provided, settings.vapi_secret):
            raise HTTPException(status_code=401, detail="invalid webhook secret")

    msg = payload.message or {}
    msg_type = msg.get("type")
    call = msg.get("call") or {}
    call_id = call.get("id")

    # Sprint 6d: webhook dedup — Vapi retries end-of-call-report on 5xx and
    # sometimes on network glitches.  Without this, disposition write-back
    # can run twice and update the Sheet counter twice.
    from app.db.idempotency import check_or_reserve_webhook_event, record_webhook_result
    tenant_id = getattr(request.state, "tenant_id", None) or "default"
    if call_id and msg_type:
        dedup_key = f"{msg_type}:{call_id}"
        cached = await check_or_reserve_webhook_event(tenant_id, "webhook:vapi", dedup_key)
        if cached is not None:
            return {"ok": True, "replay": True}

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

        await session_manager.end_session_async(f"vapi_{call_id}", tenant_id="default")
        result = {"ok": True}
        if outbound_outcome and outbound_outcome.get("reason") != "not_outbound":
            result = {"ok": True, "outbound": outbound_outcome}
        # Record dedup result so retries return the same response
        if call_id:
            record_webhook_result(tenant_id, "webhook:vapi", f"{msg_type}:{call_id}", 200, result)
        return result

    if call_id:
        record_webhook_result(tenant_id, "webhook:vapi", f"{msg_type}:{call_id}", 200, {"ok": True})
    return {"ok": True}

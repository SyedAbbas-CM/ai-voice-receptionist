from __future__ import annotations

import json
import uuid
from pathlib import Path
from threading import Lock

from app.core.config import settings
from app.db import SessionRow, TranscriptRow, BookingRow
from app.db.session import SessionLocal
from app.providers import get_llm
from packages.core_agent import ReceptionistBrain
from packages.integrations import build_sink_from_env, build_tools_for_vertical
from packages.integrations.calendar_factory import build_calendar
from packages.schemas import (
    BusinessProfile,
    CallState,
    TranscriptTurn,
    TurnRole,
)


_brains: dict[str, ReceptionistBrain] = {}
_states: dict[str, CallState] = {}
_lock = Lock()
_business_cache: BusinessProfile | None = None
_calendar_cache = None
_sink_cache = None


def load_business() -> BusinessProfile:
    global _business_cache
    if _business_cache is None:
        raw = json.loads(Path(settings.business_profile_path).read_text())
        _business_cache = BusinessProfile(**raw)
    return _business_cache


def get_calendar():
    global _calendar_cache
    if _calendar_cache is None:
        # Audit-3 fix: pass the business so FakeCalendar honours
        # BusinessProfile.hours instead of hardcoded 9-5.
        _calendar_cache = build_calendar(
            settings.calendar_backend, settings, business=load_business(),
        )
    return _calendar_cache


def get_sink():
    global _sink_cache
    if _sink_cache is None:
        # 2026-08-31 GHL-SMS wave 1: pass business so per-tenant SMS
        # flags (send_sms_on_booking, sms_confirmation_template) are
        # honored.
        _sink_cache = build_sink_from_env(
            settings.crm_sink, settings, business=load_business(),
        )
    return _sink_cache


_pii_redactor_cache = None


def _get_pii_redactor():
    """Lazy singleton — swap-able via settings.pii_redactor (noop|regex|presidio)."""
    global _pii_redactor_cache
    if _pii_redactor_cache is None:
        from packages.compliance import build_pii_redactor
        _pii_redactor_cache = build_pii_redactor(
            kind=getattr(settings, "pii_redactor", None) or "regex"
        )
    return _pii_redactor_cache


_retriever_cache = None


def _get_retriever():
    """Lazy singleton RAG retriever. Returns None if RAG is disabled or the
    backend fails to init — the brain then just doesn't get lookup_answer."""
    global _retriever_cache
    if _retriever_cache is not None:
        return _retriever_cache
    kind = (getattr(settings, "rag_retriever", None) or "sqlite").lower()
    if kind == "noop" or kind == "":
        return None
    try:
        from packages.rag import build_retriever, build_embedder
        embedder = build_embedder(
            kind=getattr(settings, "rag_embedder", None) or "local"
        )
        _retriever_cache = build_retriever(kind, embedder=embedder)
        return _retriever_cache
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("RAG retriever init failed: %s", e)
        return None


def _new_brain(business: BusinessProfile) -> ReceptionistBrain:
    llm = get_llm()
    retriever = _get_retriever()
    tools, handler = build_tools_for_vertical(
        business,
        get_calendar(),
        retriever=retriever,
        shaper_llm=llm if retriever is not None else None,
        confidence_threshold=getattr(settings, "rag_confidence_threshold", 0.7),
    )
    # Sprint 10 WIRING: pass calendar so kernel_wiring can build a
    # CommitAdapter.  Ignored unless settings.dialogue_kernel_enabled.
    return ReceptionistBrain(
        llm=llm, business=business, tools=tools, tool_handler=handler,
        calendar=get_calendar(),
    )


def start_session(tenant_id: str = "default") -> tuple[CallState, ReceptionistBrain]:
    return start_session_with_id(f"sess_{uuid.uuid4().hex[:12]}", tenant_id=tenant_id)


def start_session_with_id(
    session_id: str, tenant_id: str = "default"
) -> tuple[CallState, ReceptionistBrain]:
    """Create a live session owned by tenant_id.

    RE-AUDIT FIX 2026-08-02 (CRITICAL-01): tenant is captured at
    creation time.  Later get_session calls verify the caller owns it.
    """
    business = load_business()
    state = CallState(
        session_id=session_id,
        business_id=business.id,
        tenant_id=tenant_id,
    )
    brain = _new_brain(business)
    with _lock:
        _states[session_id] = state
        _brains[session_id] = brain
    _persist_session(state)
    return state, brain


def get_session(
    session_id: str, tenant_id: str = "default"
) -> tuple[CallState, ReceptionistBrain] | None:
    """Fetch a live session ONLY if the caller's tenant matches its owner.

    RE-AUDIT FIX 2026-08-02 (CRITICAL-01): the auditor demonstrated that
    Tenant B could submit Tenant A's session_id and receive a 200 with
    A's business state.  Now: mismatched tenant returns None (caller
    sees a 404 from the route), same as if the session didn't exist.
    """
    with _lock:
        state = _states.get(session_id)
        brain = _brains.get(session_id)
    if not (state and brain):
        return None
    # Strict tenant check — tenants can never see each other's sessions,
    # even by guessing the session_id.
    if state.tenant_id != tenant_id and tenant_id != "default":
        # Log for observability (someone trying to cross tenants is a
        # security event worth surfacing) but return the same "not found"
        # response as if the session didn't exist.
        import logging
        logging.getLogger(__name__).warning(
            "cross-tenant session access attempt: session_id=%s owner=%s caller=%s",
            session_id, state.tenant_id, tenant_id,
        )
        return None
    return state, brain


def end_session(session_id: str, tenant_id: str = "default") -> None:
    """Sync end. Persists final state but does NOT fire sink.on_call_end —
    use end_session_async for that."""
    # Ownership check before mutation
    with _lock:
        state = _states.get(session_id)
        if state and state.tenant_id != tenant_id and tenant_id != "default":
            return  # not yours — silently ignore
        state = _states.pop(session_id, None)
        _brains.pop(session_id, None)
    if state:
        from packages.schemas import CallStatus
        if state.status == CallStatus.ACTIVE:
            state.status = CallStatus.COMPLETED
        _persist_session(state, flush_transcript=True)


async def end_session_async(session_id: str, tenant_id: str = "default") -> None:
    with _lock:
        state = _states.get(session_id)
        if state and state.tenant_id != tenant_id and tenant_id != "default":
            return  # not yours — silently ignore
        state = _states.pop(session_id, None)
        _brains.pop(session_id, None)
    if not state:
        return
    from packages.schemas import CallStatus
    if state.status == CallStatus.ACTIVE:
        state.status = CallStatus.COMPLETED
    _persist_session(state, flush_transcript=True)
    try:
        await get_sink().on_call_end(state)
    except Exception:
        pass


def _persist_session(state: CallState, flush_transcript: bool = True) -> None:
    # AUDIT FIX 2026-08-04 (dial-test crash): wrap the whole DB scope
    # in the tenant contextvar.  Fixes two related failures:
    #   (a) auto-filter listener needs current_tenant to inject
    #       WHERE tenant_id = ? on ORM queries
    #   (b) tenant_guard needs to see that filter in the compiled SQL
    # Without this, calls to _persist_session from the Twilio actor
    # path crashed the whole call with CrossTenantLeakError.
    from app.db.session import set_current_tenant, reset_current_tenant
    _tenant_token = set_current_tenant(state.tenant_id)
    db = SessionLocal()
    try:
        # Explicit tenant_id filter so the tenant_guard doesn't reject
        # this lookup even if the auto-filter listener misfires.  Belt-
        # and-suspenders: contextvar + explicit filter both present.
        row = (
            db.query(SessionRow)
            .filter(SessionRow.id == state.session_id)
            .filter(SessionRow.tenant_id == state.tenant_id)
            .first()
        )
        if row is None:
            row = SessionRow(
                id=state.session_id,
                tenant_id=state.tenant_id,
                business_id=state.business_id,
                status=state.status.value if hasattr(state.status, "value") else state.status,
                started_at=state.started_at,
            )
            db.add(row)
        row.status = state.status.value if hasattr(state.status, "value") else state.status
        row.ended_at = state.ended_at
        row.extracted = state.extracted.model_dump() if state.extracted else None
        row.escalation_reason = state.escalation_reason
        # Phase 2 (2026-08-30, task #95): snapshot the wider agent
        # persona prompt ONCE at first persist. LK judges (Phase 3) use
        # this as fallback when no per-turn scope delta is present.
        # Voice-agent sets `state._opening_system_prompt` in brain.py's
        # first LLM prep. Only write if unset — sub-agent-scope prompt
        # swaps should NOT overwrite this (they land in per-turn
        # `agent_instructions_delta` instead).
        if row.opening_system_prompt is None:
            _opening = getattr(state, "_opening_system_prompt", None)
            if _opening:
                row.opening_system_prompt = _opening

        if flush_transcript:
            redactor = _get_pii_redactor()
            existing = (
                db.query(TranscriptRow)
                .filter(TranscriptRow.session_id == state.session_id)
                .filter(TranscriptRow.tenant_id == state.tenant_id)
                .count()
            )
            for turn in state.transcript[existing:]:
                # PII redaction on text + structured tool args/results before
                # they hit SQLite. Never persist raw phone/card/SSN.
                redacted_text = redactor.redact_text(turn.text or "").text
                redacted_args = None
                redacted_result = None
                if turn.tool_args:
                    redacted_args, _ = redactor.redact_dict(turn.tool_args)
                if turn.tool_result:
                    redacted_result, _ = redactor.redact_dict(turn.tool_result)
                # Phase 2 (2026-08-30, task #95): pull per-turn scope
                # delta + tool error from Turn if present. These are
                # optional attrs on Turn — populated by brain.py when
                # sub-agent scope changes or a tool errors. LK judges
                # (Phase 3) use both to grade turns under the correct
                # effective instructions.
                _delta = getattr(turn, "agent_instructions_delta", None)
                _tool_err = getattr(turn, "tool_error", None)
                db.add(TranscriptRow(
                    session_id=state.session_id,
                    tenant_id=state.tenant_id,
                    role=turn.role.value,
                    text=redacted_text,
                    timestamp=turn.timestamp,
                    tool_name=turn.tool_name,
                    tool_args=redacted_args,
                    tool_result=redacted_result,
                    agent_instructions_delta=_delta,
                    tool_error=_tool_err,
                ))
        db.commit()
    finally:
        db.close()
        reset_current_tenant(_tenant_token)


BOOKING_TOOL_NAMES = {"book_appointment", "book_reservation", "book_viewing"}


def persist_booking_from_tool(state: CallState, tool_payload: dict) -> None:
    """If any vertical's booking tool call succeeded, write a BookingRow."""
    if tool_payload.get("name") not in BOOKING_TOOL_NAMES:
        return
    result = tool_payload.get("result") or {}
    if not result.get("booked"):
        return
    event = result.get("event") or {}
    args = tool_payload.get("arguments") or {}
    from datetime import datetime as _dt
    from app.db.session import set_current_tenant, reset_current_tenant
    _tenant_token = set_current_tenant(state.tenant_id)
    db = SessionLocal()
    try:
        db.add(BookingRow(
            id=event.get("id") or f"bkg_{uuid.uuid4().hex[:12]}",
            tenant_id=state.tenant_id,
            session_id=state.session_id,
            business_id=state.business_id,
            caller_name=args.get("caller_name", ""),
            phone=args.get("phone", ""),
            service=args.get("service") or args.get("property_ref") or f"party_of_{args.get('party_size', '?')}",
            scheduled_for=_dt.fromisoformat(event["start"]) if event.get("start") else _dt.utcnow(),
            duration_minutes=30,
            status="confirmed",
            notes=args.get("notes"),
        ))
        db.commit()
    finally:
        db.close()
        reset_current_tenant(_tenant_token)


async def run_greeting(state: CallState, brain: ReceptionistBrain) -> str:
    result = await brain.greet(state)
    _persist_session(state)
    return result.reply


async def run_user_turn(
    state: CallState, brain: ReceptionistBrain, user_text: str,
    on_delta=None,
    on_tool_call=None,
    on_tool_receipt=None,
) -> dict:
    result = await brain.handle_user_turn(
        state, user_text,
        on_delta=on_delta,
        on_tool_call=on_tool_call,
        on_tool_receipt=on_tool_receipt,
    )
    sink = get_sink()
    for tool_payload in result.tool_results:
        persist_booking_from_tool(state, tool_payload)
        if tool_payload.get("name") in BOOKING_TOOL_NAMES and (tool_payload.get("result") or {}).get("booked"):
            try:
                await sink.on_booking(state, tool_payload)
            except Exception:
                pass
    _persist_session(state)
    return {
        "reply": result.reply,
        "extracted": state.extracted.model_dump(),
        "tool_results": result.tool_results,
        "escalated": result.escalated,
        "status": state.status.value if hasattr(state.status, "value") else state.status,
    }


# Convenience alias used in route imports
_ = TranscriptTurn, TurnRole

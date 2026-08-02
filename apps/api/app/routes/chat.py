from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.core import session_manager


router = APIRouter(prefix="/chat", tags=["chat"])


def _caller_tenant(request: Request) -> str:
    """Pull tenant from auth middleware, default to 'default' for
    unauthenticated widget/simulator paths (public allowlist)."""
    return getattr(request.state, "tenant_id", None) or "default"


class StartResponse(BaseModel):
    session_id: str
    greeting: str
    business_name: str


class TurnRequest(BaseModel):
    session_id: str
    text: str


class TurnResponse(BaseModel):
    reply: str
    extracted: dict
    tool_results: list[dict]
    escalated: bool
    status: str


@router.post("/start", response_model=StartResponse)
async def start_call(request: Request) -> StartResponse:
    # RE-AUDIT FIX 2026-08-02 (CRITICAL-01): session ownership set at creation
    tenant_id = _caller_tenant(request)
    state, brain = session_manager.start_session(tenant_id=tenant_id)
    greeting = await session_manager.run_greeting(state, brain)
    return StartResponse(
        session_id=state.session_id,
        greeting=greeting,
        business_name=brain.business.name,
    )


@router.post("/turn", response_model=TurnResponse)
async def caller_turn(req: TurnRequest, request: Request) -> TurnResponse:
    # RE-AUDIT FIX 2026-08-02 (CRITICAL-01): tenant ownership check.
    # Session belongs to a tenant; caller must own it.  Mismatched tenant
    # returns the same 404 as a nonexistent session — no info leak.
    tenant_id = _caller_tenant(request)
    handle = session_manager.get_session(req.session_id, tenant_id=tenant_id)
    if handle is None:
        raise HTTPException(status_code=404, detail="session not found or already ended")
    state, brain = handle
    payload = await session_manager.run_user_turn(state, brain, req.text)
    return TurnResponse(**payload)


@router.post("/end")
async def end_call(req: TurnRequest, request: Request) -> dict:
    tenant_id = _caller_tenant(request)
    await session_manager.end_session_async(req.session_id, tenant_id=tenant_id)
    return {"ended": True, "session_id": req.session_id}

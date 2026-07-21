from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core import session_manager


router = APIRouter(prefix="/chat", tags=["chat"])


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
async def start_call() -> StartResponse:
    state, brain = session_manager.start_session()
    greeting = await session_manager.run_greeting(state, brain)
    return StartResponse(
        session_id=state.session_id,
        greeting=greeting,
        business_name=brain.business.name,
    )


@router.post("/turn", response_model=TurnResponse)
async def caller_turn(req: TurnRequest) -> TurnResponse:
    handle = session_manager.get_session(req.session_id)
    if handle is None:
        raise HTTPException(status_code=404, detail="session not found or already ended")
    state, brain = handle
    payload = await session_manager.run_user_turn(state, brain, req.text)
    return TurnResponse(**payload)


@router.post("/end")
async def end_call(req: TurnRequest) -> dict:
    await session_manager.end_session_async(req.session_id)
    return {"ended": True, "session_id": req.session_id}

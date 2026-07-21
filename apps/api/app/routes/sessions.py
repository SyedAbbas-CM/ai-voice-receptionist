from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import BookingRow, SessionRow, TranscriptRow
from app.db.session import get_session


router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.get("")
def list_sessions(db: Session = Depends(get_session)) -> list[dict]:
    rows = db.query(SessionRow).order_by(SessionRow.started_at.desc()).limit(100).all()
    return [
        {
            "session_id": r.id,
            "business_id": r.business_id,
            "status": r.status,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "ended_at": r.ended_at.isoformat() if r.ended_at else None,
            "extracted": r.extracted,
            "escalation_reason": r.escalation_reason,
        }
        for r in rows
    ]


@router.get("/{session_id}")
def get_session_detail(session_id: str, db: Session = Depends(get_session)) -> dict:
    row = db.get(SessionRow, session_id)
    if not row:
        raise HTTPException(status_code=404, detail="session not found")
    turns = db.query(TranscriptRow).filter_by(session_id=session_id).order_by(TranscriptRow.id.asc()).all()
    return {
        "session_id": row.id,
        "business_id": row.business_id,
        "status": row.status,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "ended_at": row.ended_at.isoformat() if row.ended_at else None,
        "extracted": row.extracted,
        "escalation_reason": row.escalation_reason,
        "transcript": [
            {
                "role": t.role,
                "text": t.text,
                "timestamp": t.timestamp.isoformat() if t.timestamp else None,
                "tool_name": t.tool_name,
                "tool_args": t.tool_args,
                "tool_result": t.tool_result,
            }
            for t in turns
        ],
    }


@router.get("/{session_id}/bookings")
def list_session_bookings(session_id: str, db: Session = Depends(get_session)) -> list[dict]:
    rows = db.query(BookingRow).filter_by(session_id=session_id).all()
    return [
        {
            "id": r.id,
            "caller_name": r.caller_name,
            "phone": r.phone,
            "service": r.service,
            "scheduled_for": r.scheduled_for.isoformat() if r.scheduled_for else None,
            "duration_minutes": r.duration_minutes,
            "status": r.status,
            "notes": r.notes,
        }
        for r in rows
    ]

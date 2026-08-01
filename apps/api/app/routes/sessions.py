from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.db import BookingRow, SessionRow, TranscriptRow
from app.db.session import get_session
from app.middleware.auth import get_tenant_id


router = APIRouter(prefix="/sessions", tags=["sessions"])


# AUDIT FIX 2026-08-01 (SEC-002, SEC-003):
# Every session route filters by tenant_id.  A tenant only sees THEIR sessions,
# never anyone else's, no matter what session_id they guess.
#
# NOTE: SessionRow doesn't have a tenant_id column yet — that's a Sprint 6
# database migration.  Until then, we use business_id as the tenant proxy and
# only return rows whose business_id matches the authenticated tenant.  Ops
# who bring their own API_KEY without configuring per-business scoping will
# see everything under tenant "default" (which is the intended dev behavior).


def _tenant_owns_business(tenant_id: str, business_id: str | None) -> bool:
    """Interim check until real per-tenant business mapping ships.

    Rule of thumb:
      * tenant "default" (single-key dev deployments) sees everything
      * any other tenant only sees business rows where business_id == tenant_id
    """
    if tenant_id == "default":
        return True
    return (business_id or "") == tenant_id


@router.get("")
def list_sessions(request: Request, db: Session = Depends(get_session)) -> list[dict]:
    tenant = get_tenant_id(request)
    q = db.query(SessionRow)
    if tenant != "default":
        q = q.filter(SessionRow.business_id == tenant)
    rows = q.order_by(SessionRow.started_at.desc()).limit(100).all()
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
def get_session_detail(
    session_id: str, request: Request, db: Session = Depends(get_session)
) -> dict:
    tenant = get_tenant_id(request)
    row = db.get(SessionRow, session_id)
    if not row:
        raise HTTPException(status_code=404, detail="session not found")
    if not _tenant_owns_business(tenant, row.business_id):
        # 404 not 403 — don't leak existence of other tenants' sessions
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
def list_session_bookings(
    session_id: str, request: Request, db: Session = Depends(get_session)
) -> list[dict]:
    tenant = get_tenant_id(request)
    # Verify session ownership first, then return bookings
    session_row = db.get(SessionRow, session_id)
    if not session_row or not _tenant_owns_business(tenant, session_row.business_id):
        raise HTTPException(status_code=404, detail="session not found")
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

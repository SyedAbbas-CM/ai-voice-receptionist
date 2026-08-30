"""Business-owner dashboard — read-only view of a tenant's activity.

Sprint (2026-08-24): first shippable dashboard.  Business owner logs in
via the same tenant API key they use for the platform (bearer in query
string for browser convenience — see security note below).  Sees:

  * Today's bookings (upcoming + completed)
  * Recent calls (with outcome + duration)
  * Full transcript per call (click through)
  * Missed calls (call ended without a booking)

Design principles:
  * Read-only.  Zero mutation surface — no delete, no edit.  Ops
    changes go through admin routes.
  * Server-rendered HTML, no JS framework.  Ships as one Python file
    + inline CSS.  Prospects want to click and see things work; a
    React build step is friction.
  * Tenant-scoped.  Every query filters `WHERE tenant_id = :tid` via
    the auth dependency.  A tenant CAN'T see another tenant's data,
    even by URL manipulation.
  * Fast.  All queries indexed (per networking's schema audit): tenant
    + started_at, tenant + scheduled_for.
  * Zero-touch on network / DB write paths per session-split contract.

Security note on query-string auth:
  Browser bearer-in-header requires custom fetch code; for demo-scale
  ease we ALSO accept `?token=<api_key>`.  Tokens in URLs land in
  server access logs + browser history, so this is DEMO-ONLY.
  Production tenants should proxy through a signed-cookie session.
  Guarded by `settings.dashboard_allow_token_in_url` — default True
  now to unblock demo; flip to False before real customer traffic.
"""
from __future__ import annotations

import html as _html
from datetime import datetime, date, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.db import BookingRow, SessionRow, TranscriptRow
from app.db.session import get_session as _get_db_session
from app.middleware.auth import _resolve_tenant_from_db as _resolve_tenant

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


# ── auth dependency (dashboard-specific) ─────────────────────────────


def _resolve_dashboard_tenant(request: Request) -> str:
    """Resolve tenant from Authorization: Bearer header (always
    accepted) OR ?token= query param (guarded).

    **?token= security posture (2026-08-25 security-review):** query-
    string tokens land in server access logs, browser history, and
    Referer headers on outbound links.  Two guards:
      1. `settings.dashboard_allow_token_in_url` (default True for
         demo/pilot; flip to False when a production tenant onboards).
      2. Force-False when `ENVIRONMENT=production` — even a True flag
         is ignored on the production host.

    When ?token= is disabled and no Bearer header is present, we
    return a 401 with an explicit message.  Long-term (P0.3 networking
    work): signed short-lived widget tickets replace long-lived API
    keys entirely.

    Fail-closed on unknown tokens.  Returns tenant_id.
    """
    import os
    from app.core.config import settings as _settings

    bearer = ""
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        bearer = auth.removeprefix("Bearer ").strip()

    if not bearer:
        # Query-string fallback — guarded.
        env = os.environ.get("ENVIRONMENT", "development").lower()
        query_token_allowed = (
            getattr(_settings, "dashboard_allow_token_in_url", True)
            and env != "production"
        )
        if query_token_allowed:
            bearer = request.query_params.get("token", "").strip()
        else:
            # Explicit hint: log the attempt so ops sees browser widgets
            # broken by the flip; don't just return a bare 401.
            if request.query_params.get("token"):
                import logging as _l
                _l.getLogger(__name__).warning(
                    "DASHBOARD_QUERY_TOKEN_BLOCKED env=%s allow_flag=%s "
                    "remote=%s — token in URL rejected; use Authorization "
                    "header instead",
                    env,
                    getattr(_settings, "dashboard_allow_token_in_url", True),
                    (request.client.host if request.client else "?"),
                )
            raise HTTPException(
                status_code=401,
                detail=(
                    "dashboard: Authorization: Bearer header required "
                    "(query-string tokens disabled by policy)"
                ),
            )

    if not bearer:
        raise HTTPException(
            status_code=401,
            detail="dashboard: Authorization: Bearer <api-key> required",
        )
    tenant_id = _resolve_tenant(bearer)
    if not tenant_id:
        raise HTTPException(status_code=401, detail="dashboard: invalid token")
    return tenant_id


# ── data helpers ─────────────────────────────────────────────────────


def _day_bounds(iso_or_none: Optional[str]) -> tuple[datetime, datetime]:
    """Return (start, end) UTC for the requested day.  Defaults to
    today in UTC — business-timezone-aware rendering is a follow-up
    (business tz would need to load the profile per tenant which is
    a separate concern)."""
    if iso_or_none:
        try:
            d = date.fromisoformat(iso_or_none)
        except ValueError:
            d = datetime.now(timezone.utc).date()
    else:
        d = datetime.now(timezone.utc).date()
    start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


def _fetch_todays_bookings(
    db: Session, tenant_id: str, day: Optional[str] = None,
) -> list[BookingRow]:
    start, end = _day_bounds(day)
    q = (
        select(BookingRow)
        .where(and_(
            BookingRow.tenant_id == tenant_id,
            BookingRow.scheduled_for >= start,
            BookingRow.scheduled_for < end,
        ))
        .order_by(BookingRow.scheduled_for.asc())
    )
    return list(db.scalars(q, execution_options={"skip_tenant_filter": True}).all())


def _fetch_recent_sessions(
    db: Session, tenant_id: str, limit: int = 25,
) -> list[SessionRow]:
    q = (
        select(SessionRow)
        .where(SessionRow.tenant_id == tenant_id)
        .order_by(SessionRow.started_at.desc())
        .limit(limit)
    )
    return list(db.scalars(q, execution_options={"skip_tenant_filter": True}).all())


def _fetch_todays_missed(
    db: Session, tenant_id: str, day: Optional[str] = None,
) -> list[SessionRow]:
    """Sessions that started today but produced no booking."""
    start, end = _day_bounds(day)
    # Subquery: sessions with a booking.
    booked_subq = (
        select(BookingRow.session_id)
        .where(and_(
            BookingRow.tenant_id == tenant_id,
            BookingRow.scheduled_for >= start,
            BookingRow.scheduled_for < end,
        ))
        .distinct()
    )
    q = (
        select(SessionRow)
        .where(and_(
            SessionRow.tenant_id == tenant_id,
            SessionRow.started_at >= start,
            SessionRow.started_at < end,
            SessionRow.id.notin_(booked_subq),
        ))
        .order_by(SessionRow.started_at.desc())
    )
    return list(db.scalars(q, execution_options={"skip_tenant_filter": True}).all())


def _fetch_transcript(
    db: Session, tenant_id: str, session_id: str,
) -> tuple[Optional[SessionRow], list[TranscriptRow]]:
    # Bypass the auto-tenant-filter (see _fetch_recent_sessions comment).
    # Our own explicit `WHERE tenant_id == :tid` guard below is the safety.
    session = db.scalars(
        select(SessionRow).where(SessionRow.id == session_id),
        execution_options={"skip_tenant_filter": True},
    ).first()
    if session is None or session.tenant_id != tenant_id:
        return None, []
    turns = list(db.scalars(
        select(TranscriptRow)
        .where(TranscriptRow.session_id == session_id)
        .order_by(TranscriptRow.timestamp.asc()),
        execution_options={"skip_tenant_filter": True},
    ).all())
    return session, turns


# ── rendering ────────────────────────────────────────────────────────


def _esc(s: object) -> str:
    """HTML-escape any value.  None → empty string.  Never trust
    strings coming out of the DB — transcripts contain arbitrary text."""
    if s is None:
        return ""
    return _html.escape(str(s), quote=True)


def _fmt_time(dt: Optional[datetime]) -> str:
    if dt is None:
        return "—"
    return dt.strftime("%b %-d, %-I:%M %p")


def _fmt_duration(a: Optional[datetime], b: Optional[datetime]) -> str:
    if a is None or b is None:
        return "—"
    delta = b - a
    total = int(delta.total_seconds())
    if total < 60:
        return f"{total}s"
    mins = total // 60
    secs = total % 60
    return f"{mins}m {secs}s"


_BASE_CSS = """
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
         Helvetica, Arial, sans-serif; margin: 0; padding: 0;
         background: #f5f7fb; color: #1c2434; }
  header { background: #1c2434; color: #fff; padding: 14px 22px;
           display: flex; justify-content: space-between; align-items: center; }
  header h1 { margin: 0; font-size: 18px; font-weight: 600; }
  header nav a { color: #cfd6e4; margin-left: 18px; text-decoration: none;
                 font-size: 14px; }
  header nav a:hover { color: #fff; }
  main { max-width: 1100px; margin: 0 auto; padding: 22px; }
  section { background: #fff; border-radius: 10px; padding: 18px 22px;
            box-shadow: 0 1px 3px rgba(20,30,50,0.06); margin-bottom: 20px; }
  section h2 { margin: 0 0 12px; font-size: 15px; text-transform: uppercase;
               letter-spacing: 0.05em; color: #55627a; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th { text-align: left; padding: 8px 10px; border-bottom: 1px solid #dfe4ee;
       font-weight: 600; color: #55627a; font-size: 12px;
       text-transform: uppercase; letter-spacing: 0.05em; }
  td { padding: 10px; border-bottom: 1px solid #ecf0f6; vertical-align: top; }
  tr:last-child td { border-bottom: none; }
  a { color: #2563eb; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .empty { color: #8792a8; font-style: italic; padding: 16px 0; }
  .pill { display: inline-block; padding: 2px 8px; border-radius: 10px;
          font-size: 12px; font-weight: 500; }
  .pill-ok { background: #dcfce7; color: #166534; }
  .pill-warn { background: #fef3c7; color: #92400e; }
  .pill-err { background: #fee2e2; color: #991b1b; }
  .transcript { font-family: "SF Mono", Menlo, Consolas, monospace;
                font-size: 13px; line-height: 1.6; }
  .transcript .turn { padding: 6px 0; border-bottom: 1px dashed #ecf0f6; }
  .transcript .role { display: inline-block; width: 90px; color: #55627a;
                      text-transform: uppercase; font-size: 11px; }
  .transcript .role-user { color: #2563eb; }
  .transcript .role-assistant { color: #059669; }
  .transcript .role-tool { color: #9333ea; }
  .transcript .text { display: inline; }
  .meta { color: #8792a8; font-size: 12px; }
</style>
"""


def _page(title: str, tenant_id: str, token: str, body_html: str) -> str:
    """Wrap body in the standard chrome.  `token` is threaded through
    nav links so browser navigation preserves auth without JS."""
    tok_qs = f"?token={_esc(token)}" if token else ""
    return (
        "<!DOCTYPE html><html lang=en><head><meta charset=utf-8>"
        f"<title>{_esc(title)} — Reception Dashboard</title>"
        + _BASE_CSS +
        "</head><body>"
        "<header>"
        f"<h1>Reception dashboard <span class=meta>· {_esc(tenant_id)}</span></h1>"
        "<nav>"
        f"<a href=\"/dashboard/{tok_qs}\">Today</a>"
        f"<a href=\"/dashboard/calls{tok_qs}\">Recent calls</a>"
        f"<a href=\"/dashboard/bookings{tok_qs}\">All bookings</a>"
        "</nav></header>"
        "<main>" + body_html + "</main></body></html>"
    )


def _render_bookings_table(bookings: list[BookingRow]) -> str:
    if not bookings:
        return "<div class=empty>No bookings for this day yet.</div>"
    rows = []
    for b in bookings:
        status = (b.status or "confirmed").lower()
        pill = {
            "confirmed": "pill pill-ok",
            "pending": "pill pill-warn",
            "cancelled": "pill pill-err",
            "no_show": "pill pill-err",
        }.get(status, "pill")
        rows.append(
            "<tr>"
            f"<td>{_esc(_fmt_time(b.scheduled_for))}</td>"
            f"<td>{_esc(b.caller_name)}</td>"
            f"<td>{_esc(b.phone)}</td>"
            f"<td>{_esc(b.service)}</td>"
            f"<td>{b.duration_minutes} min</td>"
            f"<td><span class=\"{pill}\">{_esc(status)}</span></td>"
            "</tr>"
        )
    return (
        "<table><thead><tr>"
        "<th>When</th><th>Caller</th><th>Phone</th><th>Service</th>"
        "<th>Length</th><th>Status</th>"
        "</tr></thead><tbody>"
        + "".join(rows) +
        "</tbody></table>"
    )


def _session_id_to_call_id(session_id: str) -> str:
    """Convert stored session_id (typically 'twilio_CA...') back to
    the raw CA-SID for /trace/{call_id} URLs.  /trace's resolver
    accepts either form, but the URL reads cleaner + is easier to
    copy-paste to Twilio Console with the bare SID.

    Falls back to the original string when the shape doesn't match
    (e.g. widget sessions like 'twilio_browser_261caaf9') — /trace
    still resolves those correctly."""
    if not session_id:
        return session_id
    if session_id.startswith("twilio_"):
        return session_id[len("twilio_"):]
    return session_id


def _render_sessions_table(
    sessions: list[SessionRow], token: str,
) -> str:
    if not sessions:
        return "<div class=empty>No calls recorded yet.</div>"
    tok_qs = f"?token={_esc(token)}" if token else ""
    rows = []
    for s in sessions:
        status = (s.status or "active").lower()
        pill_cls = {
            "completed": "pill pill-ok",
            "escalated": "pill pill-warn",
            "abandoned": "pill pill-err",
            "active": "pill pill-warn",
        }.get(status, "pill")
        extracted = s.extracted or {}
        caller = extracted.get("caller_name") or "—"
        intent = extracted.get("intent") or ""
        # 2026-08-30 (task #143): humanness trace link.  Business
        # owners click straight from a call row to the /trace view
        # showing the humanness timeline (policy decisions, slot
        # capture events, service resolution, discovery, empty-
        # completion protections, etc).  Auth reuses the same
        # tenant Bearer / ?token= as this dashboard.
        _call_id_for_trace = _session_id_to_call_id(s.id)
        rows.append(
            "<tr>"
            f"<td>{_esc(_fmt_time(s.started_at))}</td>"
            f"<td>{_esc(caller)}</td>"
            f"<td>{_esc(intent)}</td>"
            f"<td>{_esc(_fmt_duration(s.started_at, s.ended_at))}</td>"
            f"<td><span class=\"{pill_cls}\">{_esc(status)}</span></td>"
            "<td>"
            f"<a href=\"/dashboard/calls/{_esc(s.id)}{tok_qs}\">Open</a>"
            " &middot; "
            f"<a href=\"/trace/{_esc(_call_id_for_trace)}{tok_qs}\" "
            f"title=\"Humanness event timeline for this call\">Trace</a>"
            "</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr>"
        "<th>Started</th><th>Caller</th><th>Intent</th>"
        "<th>Length</th><th>Status</th><th></th>"
        "</tr></thead><tbody>"
        + "".join(rows) +
        "</tbody></table>"
    )


def _render_transcript(turns: list[TranscriptRow]) -> str:
    if not turns:
        return "<div class=empty>No transcript recorded.</div>"
    out = ["<div class=transcript>"]
    for t in turns:
        role = (t.role or "user").lower()
        role_cls = f"role role-{role}"
        text = t.text or ""
        if t.tool_name:
            text = f"[{t.tool_name}] {text}"
        out.append(
            "<div class=turn>"
            f"<span class=\"{role_cls}\">{_esc(role)}</span>"
            f"<span class=text>{_esc(text)}</span>"
            "</div>"
        )
    out.append("</div>")
    return "".join(out)


# ── routes ───────────────────────────────────────────────────────────


@router.get("/", response_class=HTMLResponse)
def dashboard_home(
    request: Request,
    day: Optional[str] = Query(None, description="ISO date YYYY-MM-DD"),
    db: Session = Depends(_get_db_session),
) -> HTMLResponse:
    """Today view — bookings + missed calls at a glance."""
    tenant_id = _resolve_dashboard_tenant(request)
    token = (
        request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        or request.query_params.get("token", "").strip()
    )
    bookings = _fetch_todays_bookings(db, tenant_id, day)
    missed = _fetch_todays_missed(db, tenant_id, day)
    day_label = day or datetime.now(timezone.utc).date().isoformat()
    body = (
        f"<section><h2>Bookings — {_esc(day_label)}</h2>"
        + _render_bookings_table(bookings) + "</section>"
        f"<section><h2>Missed calls — {_esc(day_label)}</h2>"
        + _render_sessions_table(missed, token) + "</section>"
    )
    return HTMLResponse(_page("Today", tenant_id, token, body))


@router.get("/calls", response_class=HTMLResponse)
def dashboard_calls(
    request: Request,
    limit: int = Query(25, ge=1, le=100),
    db: Session = Depends(_get_db_session),
) -> HTMLResponse:
    """Recent-calls list, newest first."""
    tenant_id = _resolve_dashboard_tenant(request)
    token = (
        request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        or request.query_params.get("token", "").strip()
    )
    sessions = _fetch_recent_sessions(db, tenant_id, limit)
    body = (
        f"<section><h2>Recent calls (last {limit})</h2>"
        + _render_sessions_table(sessions, token) + "</section>"
    )
    return HTMLResponse(_page("Recent calls", tenant_id, token, body))


@router.get("/calls/{session_id}", response_class=HTMLResponse)
def dashboard_call_transcript(
    request: Request,
    session_id: str,
    db: Session = Depends(_get_db_session),
) -> HTMLResponse:
    """Full transcript for one call."""
    tenant_id = _resolve_dashboard_tenant(request)
    token = (
        request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        or request.query_params.get("token", "").strip()
    )
    session, turns = _fetch_transcript(db, tenant_id, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    extracted = session.extracted or {}
    header = (
        f"<section><h2>Call {_esc(session_id[:12])}…</h2>"
        f"<p class=meta>Started {_esc(_fmt_time(session.started_at))}"
        f" · Length {_esc(_fmt_duration(session.started_at, session.ended_at))}"
        f" · Status <b>{_esc(session.status)}</b></p>"
    )
    if extracted:
        header += (
            "<p class=meta>"
            f"Caller: {_esc(extracted.get('caller_name') or '—')}"
            f" · Phone: {_esc(extracted.get('phone') or '—')}"
            f" · Intent: {_esc(extracted.get('intent') or '—')}"
            f" · Lead score: {_esc(extracted.get('lead_score'))}"
            "</p>"
        )
    header += "</section>"
    body = header + "<section><h2>Transcript</h2>" + _render_transcript(turns) + "</section>"
    return HTMLResponse(_page(f"Call {session_id[:8]}", tenant_id, token, body))


@router.get("/bookings", response_class=HTMLResponse)
def dashboard_all_bookings(
    request: Request,
    days: int = Query(7, ge=1, le=90),
    db: Session = Depends(_get_db_session),
) -> HTMLResponse:
    """All bookings in a rolling window (default 7 days back)."""
    tenant_id = _resolve_dashboard_tenant(request)
    token = (
        request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        or request.query_params.get("token", "").strip()
    )
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    q = (
        select(BookingRow)
        .where(and_(
            BookingRow.tenant_id == tenant_id,
            BookingRow.created_at >= cutoff,
        ))
        .order_by(BookingRow.scheduled_for.desc())
    )
    bookings = list(db.scalars(
        q, execution_options={"skip_tenant_filter": True},
    ).all())
    body = (
        f"<section><h2>All bookings (last {days} days)</h2>"
        + _render_bookings_table(bookings) + "</section>"
    )
    return HTMLResponse(_page("All bookings", tenant_id, token, body))

"""Serve call recording MP3s to the reviewer console.

## Endpoint

  * `GET /admin/recordings/{call_id}.mp3` — stream the MP3 file
    stored under `settings.call_recording_dir/{tenant}/{call_id}.mp3`.

## Auth

Same admin gate as the annotator (`_require_admin`). No tenant scope —
this is an operator dashboard. Later, per-tenant reviewer flows can
add a Bearer path.

## Not in v1

  - No range requests (`Accept-Ranges: bytes`). Browsers' `<audio>`
    element handles a full-body 200 response fine for the 500KB-a-few-MB
    files we produce. Add ranged responses later if needed for scrub.
  - No streaming from Twilio while call in progress — recording only
    exists after the call has finalized.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db import SessionRow
from app.db.session import get_session
from app.routes.admin import _require_admin


log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/recordings", tags=["admin", "recordings"])


def _session_id_from_call_id(raw: str) -> str:
    """Same convention as annotate.py — twilio_ prefix if missing."""
    if raw.startswith("twilio_"):
        return raw
    return f"twilio_{raw}"


@router.get("/{call_id}.mp3")
def get_recording(
    call_id: str,
    request: Request,
    db: Session = Depends(get_session),
) -> FileResponse:
    """Stream the MP3 for a call. 404 if no recording exists."""
    _require_admin(request)

    # Accept either raw CA-SID or twilio_ CA-SID
    raw = call_id[len("twilio_"):] if call_id.startswith("twilio_") else call_id
    session_id = _session_id_from_call_id(raw)

    sess = (
        db.query(SessionRow)
        .execution_options(allow_cross_tenant=True)
        .filter(SessionRow.id == session_id)
        .one_or_none()
    )
    if sess is None:
        raise HTTPException(404, "no session with that call_id")
    if not sess.recording_path:
        raise HTTPException(404, "no recording captured for this call")

    root = Path(settings.call_recording_dir).resolve()
    # Resolve relative → absolute, then verify the resolved path is
    # still inside root_dir (prevents "../.." tricks in a stored value).
    candidate = (root / sess.recording_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        log.warning(
            "recording_path %r for call %s escapes root %s — refusing",
            sess.recording_path, raw, root,
        )
        raise HTTPException(404, "recording path invalid")
    if not candidate.exists():
        raise HTTPException(404, "recording file missing on disk")

    return FileResponse(
        candidate,
        media_type="audio/mpeg",
        # Suggested filename when the reviewer downloads via right-click.
        filename=f"{raw}.mp3",
        headers={"Cache-Control": "private, max-age=3600"},
    )

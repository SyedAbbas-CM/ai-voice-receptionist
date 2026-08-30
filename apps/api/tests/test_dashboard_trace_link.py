"""Task #143: dashboard row → /trace/{call_id} link.

Users click a call in the dashboard + get the humanness event
timeline in one hop, without copy-pasting CallSids through
scripts/trace_call.sh.
"""
from __future__ import annotations

from dataclasses import dataclass

from packages.observability.humanness_events import (
    ServiceResolutionEvent,
)


# ── unit test for the session-id → call-id converter ─────────────


def test_session_id_to_call_id_strips_twilio_prefix():
    from apps.api.app.routes.dashboard import _session_id_to_call_id
    assert _session_id_to_call_id("twilio_CAabc123") == "CAabc123"


def test_session_id_to_call_id_passes_through_widget_shape():
    """Widget sessions like 'twilio_browser_...' or bare IDs pass
    through — /trace's resolver handles them either way."""
    from apps.api.app.routes.dashboard import _session_id_to_call_id
    assert _session_id_to_call_id("browser_x") == "browser_x"
    assert _session_id_to_call_id("CAbare") == "CAbare"


def test_session_id_to_call_id_empty_ok():
    from apps.api.app.routes.dashboard import _session_id_to_call_id
    assert _session_id_to_call_id("") == ""


# ── render integration ─────────────────────────────────────


@dataclass
class _FakeSession:
    """Minimal SessionRow stand-in for render tests."""
    id: str = "twilio_CAtest123"
    tenant_id: str = "t1"
    status: str = "completed"
    started_at: object = None
    ended_at: object = None
    extracted: dict = None


def test_sessions_table_includes_trace_link_per_row():
    from apps.api.app.routes.dashboard import _render_sessions_table
    from datetime import datetime, timezone
    s = _FakeSession(
        id="twilio_CAxyz789",
        started_at=datetime.now(timezone.utc),
        ended_at=datetime.now(timezone.utc),
        extracted={"caller_name": "Test Caller", "intent": "booking"},
    )
    html = _render_sessions_table([s], token="testtoken")
    assert "/trace/CAxyz789" in html
    # Preserves existing Open link.
    assert "/dashboard/calls/twilio_CAxyz789" in html
    # Token propagated on the trace link too.
    assert "?token=testtoken" in html


def test_sessions_table_empty_still_renders():
    from apps.api.app.routes.dashboard import _render_sessions_table
    html = _render_sessions_table([], token="")
    # Non-empty message, no trace link.
    assert "No calls recorded" in html
    assert "/trace/" not in html


def test_sessions_table_no_token_omits_query_string():
    from apps.api.app.routes.dashboard import _render_sessions_table
    from datetime import datetime, timezone
    s = _FakeSession(
        id="twilio_CAno_token",
        started_at=datetime.now(timezone.utc),
        ended_at=datetime.now(timezone.utc),
        extracted={},
    )
    html = _render_sessions_table([s], token="")
    assert "/trace/CAno_token" in html
    # No stray ?token= when token empty.
    assert "?token=" not in html


def test_sessions_table_escapes_ids():
    """Session IDs are DB-controlled but always render-escape."""
    from apps.api.app.routes.dashboard import _render_sessions_table
    from datetime import datetime, timezone
    s = _FakeSession(
        id='twilio_CA<script>alert(1)</script>',
        started_at=datetime.now(timezone.utc),
        ended_at=datetime.now(timezone.utc),
        extracted={},
    )
    html = _render_sessions_table([s], token="")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html

"""Annotation Dashboard Phase 1 acceptance (task #94).

Tests:
  1. Auth: admin token required for every endpoint
  2. 404 on unknown call_id
  3. Index page renders + lists recent calls
  4. Annotation form renders + shows transcript turns
  5. POST /save creates a new annotation
  6. POST /save updates existing annotation (upsert)
  7. GET /{call_id}/json returns saved payload
  8. is_gold toggle works
  9. Per-turn tags round-trip through the form → save → form again
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest
from starlette.testclient import TestClient


ADMIN_TOKEN = "test-admin-token-phase1"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("API_AUTH_ENFORCE", "false")
    monkeypatch.setenv("ADMIN_TOKEN", ADMIN_TOKEN)


def _client():
    from app.main import create_app
    return TestClient(create_app())


def _hdr():
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def _seed_call(db, call_id: str, tenant_id: str = "clinic", n_turns: int = 4) -> str:
    from app.db import SessionRow, TranscriptRow
    from app.db.session import set_current_tenant, reset_current_tenant

    session_id = f"twilio_{call_id}"
    tok = set_current_tenant(tenant_id)
    try:
        db.add(SessionRow(
            id=session_id, tenant_id=tenant_id,
            business_id="clinic-main", status="active",
            started_at=datetime.now(timezone.utc),
        ))
        db.flush()
        for i in range(n_turns):
            role = "user" if i % 2 == 0 else "assistant"
            db.add(TranscriptRow(
                tenant_id=tenant_id, session_id=session_id,
                role=role, text=f"turn {i} {role}",
                timestamp=datetime.now(timezone.utc),
            ))
        db.commit()
    finally:
        reset_current_tenant(tok)
    return session_id


# ─── 1. Auth ────────────────────────────────────────────────────────────────


def test_index_requires_admin():
    with _client() as c:
        r = c.get("/admin/annotate")
    assert r.status_code == 401


def test_form_requires_admin():
    with _client() as c:
        r = c.get("/admin/annotate/CAxyz")
    assert r.status_code == 401


def test_save_requires_admin():
    with _client() as c:
        r = c.post("/admin/annotate/CAxyz/save", data={"verdict": "win"})
    assert r.status_code == 401


def test_json_requires_admin():
    with _client() as c:
        r = c.get("/admin/annotate/CAxyz/json")
    assert r.status_code == 401


# ─── 2. 404 on unknown call ──────────────────────────────────────────────────


def test_form_404_on_unknown_call():
    with _client() as c:
        r = c.get("/admin/annotate/CAnothing/nope", headers=_hdr())
    # Path routing takes 'nope' as a subroute, so use a bare CA-SID that
    # doesn't exist:
    with _client() as c:
        r = c.get("/admin/annotate/CAdoesnotexist_xyz123", headers=_hdr())
    assert r.status_code == 404


def test_json_404_on_no_annotation():
    from app.db.session import SessionLocal
    call_id = f"CAnoann{uuid.uuid4().hex[:8]}"
    with SessionLocal() as db:
        _seed_call(db, call_id)
    # Session exists but no annotation yet
    with _client() as c:
        r = c.get(f"/admin/annotate/{call_id}/json", headers=_hdr())
    assert r.status_code == 404


# ─── 3. Index page ──────────────────────────────────────────────────────────


def test_index_renders_html():
    with _client() as c:
        r = c.get("/admin/annotate", headers=_hdr())
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Call annotations" in r.text


def test_index_lists_recent_call():
    from app.db.session import SessionLocal
    call_id = f"CAidx{uuid.uuid4().hex[:8]}"
    with SessionLocal() as db:
        _seed_call(db, call_id)
    with _client() as c:
        r = c.get("/admin/annotate", headers=_hdr())
    assert r.status_code == 200
    assert call_id in r.text


# ─── 4. Annotation form ─────────────────────────────────────────────────────


def test_form_renders_with_turns():
    from app.db.session import SessionLocal
    call_id = f"CAform{uuid.uuid4().hex[:8]}"
    with SessionLocal() as db:
        _seed_call(db, call_id, n_turns=6)
    with _client() as c:
        r = c.get(f"/admin/annotate/{call_id}", headers=_hdr())
    assert r.status_code == 200
    # 6 turns → 6 turn-blocks
    assert r.text.count('class="turn ') == 6
    # Form action is set
    assert f"/admin/annotate/{call_id}/save" in r.text
    # Default verdict is "unreviewed"
    assert 'value="unreviewed" checked' in r.text


# ─── 5. POST save creates new ───────────────────────────────────────────────


def test_save_creates_new_annotation():
    from app.db.session import SessionLocal
    from app.db import CallAnnotation

    call_id = f"CAcreate{uuid.uuid4().hex[:8]}"
    with SessionLocal() as db:
        _seed_call(db, call_id)

    with _client() as c:
        r = c.post(
            f"/admin/annotate/{call_id}/save",
            data={
                "verdict": "win",
                "notes": "great booking flow",
                "reviewer_id": "az@test",
                "tag_1_great_response": "1",
                "comment_1": "very natural",
            },
            headers=_hdr(),
            follow_redirects=False,
        )
    assert r.status_code == 303, r.text
    assert r.headers["location"] == f"/admin/annotate/{call_id}"

    # Verify persisted
    with SessionLocal() as db:
        ann = db.query(CallAnnotation).filter(
            CallAnnotation.call_id == call_id
        ).one()
    assert ann.verdict == "win"
    assert ann.notes == "great booking flow"
    assert ann.reviewer_id == "az@test"
    assert ann.is_gold is False
    assert ann.turn_tags is not None
    # One entry per tag; comment attaches to first tag on that turn
    assert any(
        t["turn_idx"] == 1 and t["tag"] == "great_response" and t["comment"] == "very natural"
        for t in ann.turn_tags
    )


# ─── 6. POST save is upsert (idempotent) ────────────────────────────────────


def test_save_updates_existing_annotation():
    from app.db.session import SessionLocal
    from app.db import CallAnnotation

    call_id = f"CAupsert{uuid.uuid4().hex[:8]}"
    with SessionLocal() as db:
        _seed_call(db, call_id)

    with _client() as c:
        # First save
        c.post(f"/admin/annotate/{call_id}/save",
               data={"verdict": "fail", "notes": "v1"}, headers=_hdr(),
               follow_redirects=False)
        # Second save
        c.post(f"/admin/annotate/{call_id}/save",
               data={"verdict": "win", "notes": "v2"}, headers=_hdr(),
               follow_redirects=False)

    with SessionLocal() as db:
        anns = db.query(CallAnnotation).filter(
            CallAnnotation.call_id == call_id
        ).all()
    assert len(anns) == 1, "should upsert, not create duplicates"
    assert anns[0].verdict == "win"
    assert anns[0].notes == "v2"


# ─── 7. JSON endpoint returns saved payload ─────────────────────────────────


def test_json_returns_saved_payload():
    from app.db.session import SessionLocal
    call_id = f"CAjson{uuid.uuid4().hex[:8]}"
    with SessionLocal() as db:
        _seed_call(db, call_id)

    with _client() as c:
        c.post(f"/admin/annotate/{call_id}/save",
               data={"verdict": "mixed", "notes": "okay-ish"},
               headers=_hdr(), follow_redirects=False)
        r = c.get(f"/admin/annotate/{call_id}/json", headers=_hdr())

    assert r.status_code == 200
    body = r.json()
    assert body["call_id"] == call_id
    assert body["verdict"] == "mixed"
    assert body["notes"] == "okay-ish"
    assert body["tenant_id"] == "clinic"
    assert body["is_gold"] is False


# ─── 8. is_gold toggle ──────────────────────────────────────────────────────


def test_is_gold_saves_and_persists():
    from app.db.session import SessionLocal
    from app.db import CallAnnotation

    call_id = f"CAgold{uuid.uuid4().hex[:8]}"
    with SessionLocal() as db:
        _seed_call(db, call_id)

    with _client() as c:
        c.post(f"/admin/annotate/{call_id}/save",
               data={"verdict": "win", "is_gold": "1"},
               headers=_hdr(), follow_redirects=False)

    with SessionLocal() as db:
        ann = db.query(CallAnnotation).filter(
            CallAnnotation.call_id == call_id
        ).one()
    assert ann.is_gold is True


# ─── 9. Per-turn tags round-trip ────────────────────────────────────────────


def test_per_turn_tags_render_after_save():
    from app.db.session import SessionLocal

    call_id = f"CAtags{uuid.uuid4().hex[:8]}"
    with SessionLocal() as db:
        _seed_call(db, call_id, n_turns=5)

    with _client() as c:
        # Save with tags on turns 0, 2, 3
        c.post(f"/admin/annotate/{call_id}/save", data={
            "verdict": "mixed",
            "tag_0_great_response": "1",
            "tag_2_wrong_service_asked": "1",
            "comment_2": "should have asked follow-up-of-what",
            "tag_3_dead_air": "1",
        }, headers=_hdr(), follow_redirects=False)

        # Reload the form
        r = c.get(f"/admin/annotate/{call_id}", headers=_hdr())

    assert r.status_code == 200
    # Checkboxes for those tags should be re-checked
    assert 'name="tag_0_great_response" value="1" checked' in r.text
    assert 'name="tag_2_wrong_service_asked" value="1" checked' in r.text
    assert 'name="tag_3_dead_air" value="1" checked' in r.text
    # Comment for turn 2 should be pre-filled
    assert 'value="should have asked follow-up-of-what"' in r.text


def test_accepts_twilio_prefix_in_url():
    """URL should accept either raw CA-SID or 'twilio_CA...' — reviewers
    might paste either."""
    from app.db.session import SessionLocal
    call_id = f"CAprefix{uuid.uuid4().hex[:8]}"
    with SessionLocal() as db:
        _seed_call(db, call_id)
    with _client() as c:
        raw = c.get(f"/admin/annotate/{call_id}", headers=_hdr())
        prefixed = c.get(f"/admin/annotate/twilio_{call_id}", headers=_hdr())
    assert raw.status_code == 200
    assert prefixed.status_code == 200

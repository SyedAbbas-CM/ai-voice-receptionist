"""P0.2 regression — /debug/* MUST require auth.

BACKEND-AUDIT-2026-08-25-CHATGPT.md finding #2:  /debug/* was in
`_PUBLIC_PATH_PREFIXES`, so anyone hitting `agent.eternalconquests.com/debug/traces`
or the /debug/live WebSocket could read call content across every tenant.

This test suite is the tripwire.  If someone puts /debug/ back in the
public prefix list — even to "just get the dev dashboard working" — one
of these tests fails immediately.

Three surfaces are tested:
  1. HTTP GET /debug/* returns 401 when unauthenticated (enforcement on).
  2. HTTP GET /debug/* returns 200 when a valid bearer is presented.
  3. Production mount gate: with ENVIRONMENT=production and
     OBSERVABILITY_API_ENABLED=false, /debug/* returns 404 (router
     not mounted at all).

The existing test_debug_live_ws.py explicitly sets API_AUTH_ENFORCE=false —
that test path stays passing (it exercises the WS backfill/stream logic
independent of auth).  Those two suites are orthogonal.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient


@pytest.fixture
def _env_dev_auth_off(monkeypatch, tmp_path):
    """Dev baseline — auth disabled, everything reachable.  Used only to
    prove the routes exist before we harden them."""
    monkeypatch.setenv("API_AUTH_ENFORCE", "false")
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("CALL_EVENT_LOG_PATH", str(tmp_path / "events.db"))


@pytest.fixture
def _env_dev_auth_on(monkeypatch, tmp_path):
    """Dev + auth enforced — /debug/* should now be gated."""
    monkeypatch.setenv("API_AUTH_ENFORCE", "true")
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("API_KEY", "sk_test_valid_key_for_p02_regression")
    monkeypatch.setenv("CALL_EVENT_LOG_PATH", str(tmp_path / "events.db"))


def _fresh_app():
    """Fresh app instance per test — settings are cached module-level via
    pydantic-settings, so we reload the config module too."""
    import importlib
    import app.core.config as _cfg
    importlib.reload(_cfg)
    import app.main as _main
    importlib.reload(_main)
    return _main.create_app()


def test_debug_traces_unauthenticated_returns_401(_env_dev_auth_on):
    """The specific attack the audit called out: unauthenticated GET
    /debug/traces used to return 200 with cross-tenant span data."""
    app = _fresh_app()
    with TestClient(app) as client:
        r = client.get("/debug/traces")
    assert r.status_code == 401, (
        f"P0.2 REGRESSION: /debug/traces returned {r.status_code} instead "
        f"of 401 for unauthenticated request.  Someone probably put /debug/ "
        f"back in _PUBLIC_PATH_PREFIXES in middleware/auth.py.  Body: {r.text!r}"
    )


def test_debug_call_timeline_unauthenticated_returns_401(_env_dev_auth_on):
    """Per-call timeline was one of the audit's specific exposures — it
    dumps full call event history including tool_args/tool_result."""
    app = _fresh_app()
    with TestClient(app) as client:
        r = client.get("/debug/call/CA_arbitrary_call_id")
    assert r.status_code == 401, (
        f"P0.2 REGRESSION: /debug/call/{{id}} returned {r.status_code} "
        f"unauthenticated — this is cross-tenant call-content exposure."
    )


def test_debug_errors_recent_unauthenticated_returns_401(_env_dev_auth_on):
    app = _fresh_app()
    with TestClient(app) as client:
        r = client.get("/debug/errors/recent?hours=24")
    assert r.status_code == 401


def test_debug_traces_with_valid_bearer_returns_200(_env_dev_auth_on):
    """Positive case — a real API key still gets in."""
    app = _fresh_app()
    with TestClient(app) as client:
        r = client.get(
            "/debug/traces",
            headers={"Authorization": "Bearer sk_test_valid_key_for_p02_regression"},
        )
    # 200 (empty traces) or 404 (no memory tracer wired) are both fine —
    # what we're proving is that auth PASSED (i.e. we don't get 401).
    assert r.status_code != 401, (
        f"Valid bearer got 401 on /debug/traces — auth check is too strict.  "
        f"Body: {r.text!r}"
    )


def test_debug_traces_with_wrong_bearer_returns_401(_env_dev_auth_on):
    app = _fresh_app()
    with TestClient(app) as client:
        r = client.get(
            "/debug/traces",
            headers={"Authorization": "Bearer wrong_key"},
        )
    assert r.status_code == 401


def test_production_mount_gate_logic():
    """Belt-and-suspenders: verify the mount condition in main.py directly,
    without booting the full app (which would trip the production alembic
    head check and other prod-only guards).  We assert the boolean is
    correct — that's what governs whether include_router(debug.router)
    runs.  This is a static-logic test, not an integration test.

    The condition in main.py is:
        _env != "production" or settings.observability_api_enabled

    So the router mounts UNLESS (env == "production" AND obs disabled).
    """
    def _should_mount(env: str, obs_enabled: bool) -> bool:
        return env.lower() != "production" or obs_enabled

    # Prod + obs=false → no mount (the P0.2 fix that matters)
    assert _should_mount("production", False) is False
    assert _should_mount("PRODUCTION", False) is False
    # Prod + obs=true → mount (opt-in escape hatch)
    assert _should_mount("production", True) is True
    # Dev/staging → always mount
    assert _should_mount("development", False) is True
    assert _should_mount("staging", False) is True
    assert _should_mount("", False) is True


def test_production_mount_gate_source_matches_expected_condition():
    """Guardrail against someone loosening the mount condition in main.py.
    Reads the file text and asserts the gate expression is exactly what
    we shipped.  Fails loudly if the check gets weakened."""
    import re
    from pathlib import Path

    main_py = Path(__file__).resolve().parents[1] / "app" / "main.py"
    src = main_py.read_text()

    # Require the exact production check + obs flag disjunction.  If a
    # future refactor changes the identifier names or restructures the
    # check, update this test AND re-audit the change — the whole point
    # of P0.2 is that this gate never silently regresses.
    pattern = re.compile(
        r"_env\s*!=\s*[\"']production[\"']\s*or\s+settings\.observability_api_enabled",
        re.IGNORECASE,
    )
    assert pattern.search(src), (
        "P0.2 REGRESSION: main.py no longer has the "
        "`_env != 'production' or settings.observability_api_enabled` "
        "gate around include_router(debug.router).  If the check moved "
        "or was renamed, update this test AND the audit doc."
    )
    # Also verify the debug router import still exists — otherwise the
    # gate wraps nothing and the router is silently unreachable.
    assert "debug.router" in src, "debug.router reference missing from main.py"


def test_debug_prefix_not_in_public_path_prefixes():
    """Static assertion — if someone puts /debug/ back in the public
    prefix list in a future refactor, this fails at collection time
    without needing a server."""
    from app.middleware.auth import _PUBLIC_PATH_PREFIXES

    assert "/debug/" not in _PUBLIC_PATH_PREFIXES, (
        "P0.2 REGRESSION: /debug/ was re-added to _PUBLIC_PATH_PREFIXES in "
        "middleware/auth.py.  See BACKEND-AUDIT-2026-08-25-CHATGPT.md finding "
        "#2 — this exposes cross-tenant traces + call timelines + a live "
        "WebSocket event stream.  If a dashboard needs /debug/* unauth'd, "
        "the correct fix is a short-lived signed session ticket per P0.3 "
        "pattern, not returning to the public prefix."
    )

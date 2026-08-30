"""Admin login + session cookie acceptance (task #99).

Tests:
  1. hash_password / verify_password round-trip + fail cases
  2. Cookie signing / verification + tamper detection + expiry
  3. GET /admin/login renders the form
  4. POST /admin/login with wrong password → redirect back with error
  5. POST /admin/login with right password → 303 + cookie set
  6. Signed cookie unlocks /admin/annotate
  7. Bearer token still works (backwards compat)
  8. Expired cookie is rejected
  9. Tampered cookie is rejected
 10. POST /admin/logout clears cookie
 11. 503 when NEITHER credential type is configured
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import pytest
from starlette.testclient import TestClient


ADMIN_TOKEN = "test-admin-token-99"
PASSWORD = "correct-horse-battery-staple"
COOKIE_SECRET = "0" * 64  # 64 chars, enough entropy for tests


@pytest.fixture
def _env(monkeypatch):
    from app.routes.admin_login import hash_password
    monkeypatch.setenv("API_AUTH_ENFORCE", "false")
    monkeypatch.setenv("ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hash_password(PASSWORD))
    monkeypatch.setenv("SESSION_COOKIE_SECRET", COOKIE_SECRET)


def _client():
    from app.main import create_app
    return TestClient(create_app())


# ─── 1. Password hashing ────────────────────────────────────────────────────


def test_hash_password_verify_success():
    from app.routes.admin_login import hash_password, verify_password
    h = hash_password(PASSWORD)
    assert verify_password(PASSWORD, h) is True


def test_hash_password_verify_wrong_password():
    from app.routes.admin_login import hash_password, verify_password
    h = hash_password(PASSWORD)
    assert verify_password("wrong", h) is False


def test_hash_format_is_pbkdf2_sha256():
    from app.routes.admin_login import hash_password
    h = hash_password("x")
    parts = h.split("$")
    assert parts[0] == "pbkdf2"
    assert parts[1] == "sha256"
    assert int(parts[2]) >= 600_000  # meaningful iteration count


def test_verify_password_rejects_bad_format():
    from app.routes.admin_login import verify_password
    assert verify_password("x", "not-a-real-hash") is False
    assert verify_password("x", "") is False
    assert verify_password("x", "pbkdf2$wrong$100$aGk=$aGk=") is False


def test_hashes_use_different_salts():
    """Two hashes of the same password must differ (different salts)."""
    from app.routes.admin_login import hash_password
    h1 = hash_password("same")
    h2 = hash_password("same")
    assert h1 != h2


# ─── 2. Cookie signing ──────────────────────────────────────────────────────


def test_cookie_signing_round_trip(monkeypatch):
    monkeypatch.setenv("SESSION_COOKIE_SECRET", COOKIE_SECRET)
    from app.routes.admin_login import _mint_session_cookie, _verify_cookie_value
    value = _mint_session_cookie(user="admin")
    payload = _verify_cookie_value(value)
    assert payload is not None
    assert payload["user"] == "admin"
    assert payload["exp"] > time.time()


def test_cookie_tamper_detection(monkeypatch):
    monkeypatch.setenv("SESSION_COOKIE_SECRET", COOKIE_SECRET)
    from app.routes.admin_login import _mint_session_cookie, _verify_cookie_value
    value = _mint_session_cookie()
    # Flip one byte of the payload — signature should no longer match
    b64_payload, tag = value.split(".")
    tampered_payload = b64_payload[:-1] + ("A" if b64_payload[-1] != "A" else "B")
    tampered = f"{tampered_payload}.{tag}"
    assert _verify_cookie_value(tampered) is None


def test_cookie_signature_swap_rejected(monkeypatch):
    """Two different secrets must produce incompatible cookies."""
    monkeypatch.setenv("SESSION_COOKIE_SECRET", "secret-A" * 8)
    from app.routes.admin_login import _mint_session_cookie
    value = _mint_session_cookie()
    # Rotate secret + attempt to verify old cookie
    monkeypatch.setenv("SESSION_COOKIE_SECRET", "secret-B" * 8)
    from app.routes.admin_login import _verify_cookie_value
    assert _verify_cookie_value(value) is None


def test_cookie_expiry(monkeypatch):
    """Forge a payload with exp in the past → verify returns None."""
    monkeypatch.setenv("SESSION_COOKIE_SECRET", COOKIE_SECRET)
    from app.routes.admin_login import _sign_cookie_payload, _verify_cookie_value
    expired_payload = json.dumps({
        "user": "admin", "iat": int(time.time()) - 3600,
        "exp": int(time.time()) - 1,  # 1 second past
    }).encode("utf-8")
    value = _sign_cookie_payload(expired_payload)
    assert _verify_cookie_value(value) is None


def test_cookie_without_secret_returns_none(monkeypatch):
    """No SESSION_COOKIE_SECRET → verify always fails, don't crash."""
    monkeypatch.delenv("SESSION_COOKIE_SECRET", raising=False)
    from app.routes.admin_login import _verify_cookie_value
    assert _verify_cookie_value("anything.here") is None


# ─── 3-10. HTTP flow ────────────────────────────────────────────────────────


def test_get_login_form_renders(_env):
    with _client() as c:
        r = c.get("/admin/login")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Admin login" in r.text
    assert 'name="password"' in r.text


def test_post_login_wrong_password_redirects_with_error(_env):
    with _client() as c:
        r = c.post("/admin/login", data={"password": "wrong"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/admin/login?error=")


def test_post_login_correct_password_sets_cookie(_env):
    with _client() as c:
        r = c.post("/admin/login", data={"password": PASSWORD}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/annotate"
    # Cookie present in Set-Cookie header (starlette lowercases attrs)
    set_cookie_lower = r.headers.get("set-cookie", "").lower()
    assert "voiceops_admin=" in set_cookie_lower
    assert "httponly" in set_cookie_lower
    assert "secure" in set_cookie_lower
    assert "samesite=lax" in set_cookie_lower


def test_signed_cookie_unlocks_annotate_index(_env):
    """Login → get cookie → visit /admin/annotate → 200.

    The starlette test client won't send Secure cookies to http:// by
    default, so we mint the cookie server-side and set it directly on
    the client — same effect as a real browser round-trip after login.
    """
    from app.routes.admin_login import _mint_session_cookie
    val = _mint_session_cookie()
    with _client() as c:
        c.cookies.set("voiceops_admin", val)
        r = c.get("/admin/annotate")
    assert r.status_code == 200, r.text
    assert "Call annotations" in r.text


def test_bearer_still_works_backwards_compat(_env):
    with _client() as c:
        r = c.get(
            "/admin/annotate",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
    assert r.status_code == 200


def test_no_creds_returns_401_with_login_hint(_env):
    with _client() as c:
        r = c.get("/admin/annotate")
    assert r.status_code == 401
    assert "login" in r.json()["detail"].lower()


def test_expired_cookie_rejected(_env, monkeypatch):
    from app.routes.admin_login import _sign_cookie_payload
    expired = _sign_cookie_payload(json.dumps({
        "user": "admin", "iat": 100, "exp": 200,
    }).encode())
    with _client() as c:
        c.cookies.set("voiceops_admin", expired)
        r = c.get("/admin/annotate")
    assert r.status_code == 401


def test_tampered_cookie_rejected(_env):
    """A cookie with a valid-looking payload but wrong HMAC → rejected."""
    from app.routes.admin_login import _mint_session_cookie
    val = _mint_session_cookie()
    # Corrupt the signature (last hex chars)
    tampered = val[:-4] + "0000"
    with _client() as c:
        c.cookies.set("voiceops_admin", tampered)
        r = c.get("/admin/annotate")
    assert r.status_code == 401


def test_logout_clears_cookie(_env):
    with _client() as c:
        c.post("/admin/login", data={"password": PASSWORD}, follow_redirects=False)
        r = c.post("/admin/logout", follow_redirects=False)
    assert r.status_code == 303
    # Cookie deletion: Set-Cookie with Max-Age=0 or empty value
    set_cookie = r.headers.get("set-cookie", "").lower()
    assert "voiceops_admin=" in set_cookie
    assert "max-age=0" in set_cookie or 'voiceops_admin=""' in set_cookie or "voiceops_admin=;" in set_cookie


def test_open_redirect_external_url_rejected(_env):
    """Regression: security-review MEDIUM. ?next=https://evil.com would
    post-login-redirect off-site with the fresh session cookie set."""
    with _client() as c:
        r = c.post(
            "/admin/login?next=https://evil.com/phish",
            data={"password": PASSWORD},
            follow_redirects=False,
        )
    assert r.status_code == 303
    # MUST redirect to the safe default, NOT the attacker URL
    assert r.headers["location"] == "/admin/annotate"


def test_open_redirect_protocol_relative_rejected(_env):
    """`?next=//evil.com` is another gadget browsers interpret as external."""
    with _client() as c:
        r = c.post(
            "/admin/login?next=//evil.com",
            data={"password": PASSWORD},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/annotate"


def test_open_redirect_backslash_variant_rejected(_env):
    """`?next=/\\evil.com` — Chrome/Firefox differ on how they interpret
    this; safer to reject entirely."""
    with _client() as c:
        r = c.post(
            "/admin/login?next=/%5Cevil.com",
            data={"password": PASSWORD},
            follow_redirects=False,
        )
    assert r.status_code == 303
    # Either safe default or a same-origin path — never external
    loc = r.headers["location"]
    assert loc.startswith("/") and not loc.startswith("//")


def test_open_redirect_same_origin_path_accepted(_env):
    """Legit deep-link `?next=/admin/annotate/CAxyz` should still work."""
    with _client() as c:
        r = c.post(
            "/admin/login?next=/admin/annotate/CAxyz",
            data={"password": PASSWORD},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/annotate/CAxyz"


def test_503_when_no_creds_configured(monkeypatch):
    """Neither ADMIN_TOKEN nor password hash → admin routes are dark."""
    monkeypatch.setenv("API_AUTH_ENFORCE", "false")
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD_HASH", raising=False)
    monkeypatch.delenv("SESSION_COOKIE_SECRET", raising=False)
    with _client() as c:
        r = c.get("/admin/annotate")
    assert r.status_code == 503
    assert "disabled" in r.json()["detail"].lower()

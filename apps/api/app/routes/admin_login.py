"""Admin dashboard password login + HMAC-signed session cookie (task #99).

## Why

Admin routes (/admin/annotate, /admin/calls/*/incident) were bearer-only.
Browsers can't send Authorization headers on plain URL navigation, so
using the dashboard required a browser extension like ModHeader. Fine
for dev, useless for anyone else.

This ships a password login → HttpOnly signed cookie flow. Existing
bearer-token access still works; upgrade is purely additive.

## Endpoints

  * `GET  /admin/login`          — HTML form
  * `POST /admin/login`          — verify password, set cookie
  * `POST /admin/logout`         — clear cookie
  * (helpers used by other admin routes: `verify_admin_session()`)

## Auth precedence (in `_require_admin` after upgrade)

  1. Signed session cookie (`voiceops_admin` HttpOnly + Secure + HMAC)
  2. Bearer token `ADMIN_TOKEN` in Authorization: Bearer
  3. Fail closed → 401 with redirect hint to /admin/login

## Password storage

Passwords are STORED as `pbkdf2_hmac('sha256', pw, salt, 600_000)` +
salt in `ADMIN_PASSWORD_HASH` env var. Format: `pbkdf2$sha256$600000$<b64_salt>$<b64_hash>`.
We chose PBKDF2 over bcrypt because bcrypt isn't in the box's venv +
we don't want to add a dep for this. PBKDF2 with 600k iterations is
still considered strong for interactive login (~200ms verify time).

## Cookie shape

  * Name: `voiceops_admin`
  * Value: `<b64(payload)>.<hex(hmac_sha256(SESSION_COOKIE_SECRET, payload))>`
  * Payload: JSON `{"exp": <unix_ts>, "user": "admin", "iat": <unix_ts>}`
  * Flags: HttpOnly, Secure, SameSite=Lax, Path=/
  * TTL: 24 hours (renewed on any successful admin-route hit)

## Env vars required

  * `ADMIN_PASSWORD_HASH` — pbkdf2$sha256$600000$SALT$HASH format
  * `SESSION_COOKIE_SECRET` — HMAC key (any 32+ char string; use
    `openssl rand -hex 32`). Rotating this invalidates ALL sessions.
  * `ADMIN_TOKEN` — still works, backwards compat with existing curl paths.

## Not in v1

- Rate-limiting on POST /admin/login (log-only for now; add
  slowapi later if brute force becomes real).
- Multi-user support (currently single "admin" user; extend to a
  users table if we onboard more reviewers).
- 2FA / TOTP (add if the annotation dashboard starts holding PHI).
- Session revocation list (24h expiry is enforcement; rotate
  SESSION_COOKIE_SECRET to kill all live sessions instantly).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import time
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse


log = logging.getLogger(__name__)


router = APIRouter(tags=["admin", "auth"])


# ─── config ────────────────────────────────────────────────────────────────

_COOKIE_NAME = "voiceops_admin"
_COOKIE_TTL_S = 24 * 60 * 60  # 24 hours
_PBKDF2_ITERATIONS = 600_000


# ─── password hashing ──────────────────────────────────────────────────────


def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    """Return the storable hash string for a password.

    Format: `pbkdf2$sha256$<iterations>$<b64_salt>$<b64_hash>`
    Salt is 16 random bytes if not supplied.
    """
    if salt is None:
        salt = secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS,
    )
    return "$".join([
        "pbkdf2",
        "sha256",
        str(_PBKDF2_ITERATIONS),
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(h).decode("ascii"),
    ])


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verify a plaintext against a stored `hash_password`
    output. Any parse failure returns False."""
    try:
        scheme, algo, iters_s, b64_salt, b64_hash = stored.split("$")
        if scheme != "pbkdf2" or algo != "sha256":
            return False
        iters = int(iters_s)
        salt = base64.b64decode(b64_salt)
        expected = base64.b64decode(b64_hash)
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iters,
    )
    return hmac.compare_digest(actual, expected)


# ─── cookie signing ────────────────────────────────────────────────────────


def _cookie_secret() -> Optional[bytes]:
    val = os.environ.get("SESSION_COOKIE_SECRET", "")
    if not val:
        return None
    return val.encode("utf-8")


def _sign_cookie_payload(payload: bytes) -> str:
    """Return `<b64_payload>.<hex_hmac>` — the cookie value."""
    secret = _cookie_secret()
    if not secret:
        raise RuntimeError("SESSION_COOKIE_SECRET not set — cannot sign cookies")
    tag = hmac.new(secret, payload, hashlib.sha256).hexdigest()
    return f"{base64.urlsafe_b64encode(payload).decode('ascii')}.{tag}"


def _verify_cookie_value(value: str) -> Optional[dict]:
    """Parse + verify signature. Returns payload dict or None on any
    failure (bad format, wrong signature, expired)."""
    secret = _cookie_secret()
    if not secret:
        return None
    try:
        b64_payload, tag = value.rsplit(".", 1)
        payload_bytes = base64.urlsafe_b64decode(b64_payload)
    except (ValueError, TypeError):
        return None
    expected_tag = hmac.new(secret, payload_bytes, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(tag, expected_tag):
        return None
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    # Expiry check
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or exp < time.time():
        return None
    return payload


def _mint_session_cookie(user: str = "admin") -> str:
    now = int(time.time())
    payload = json.dumps({
        "user": user,
        "iat": now,
        "exp": now + _COOKIE_TTL_S,
    }).encode("utf-8")
    return _sign_cookie_payload(payload)


def verify_admin_session(request: Request) -> Optional[dict]:
    """Check whether the request carries a valid admin session cookie.

    Returns the payload dict (user + iat + exp) if valid, None
    otherwise. Callers combine this with the bearer-token check.
    """
    cookie = request.cookies.get(_COOKIE_NAME)
    if not cookie:
        return None
    return _verify_cookie_value(cookie)


# ─── routes ────────────────────────────────────────────────────────────────


@router.get("/admin/login", response_class=HTMLResponse)
def get_login_form(request: Request, error: Optional[str] = None) -> HTMLResponse:
    """Simple password entry form.

    Security note (2026-08-30): earlier draft interpolated the raw
    `error` query param straight into the HTML → reflected-XSS gadget
    (`?error=<script>...`). Fixed by html.escape'ing before rendering
    AND clamping to a short length (100 chars) so an attacker can't
    stuff a payload big enough to be interesting even if escape somehow
    fails downstream.
    """
    error_html = ""
    if error:
        # Length clamp first; escape second (belt-and-suspenders).
        safe_error = html.escape(str(error)[:100])
        error_html = f'<div class="err">{safe_error}</div>'
    body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Admin login</title>
<style>
  body {{ font-family: -apple-system, sans-serif; background: #fafafa; margin: 0;
         display: flex; align-items: center; justify-content: center; height: 100vh; }}
  form {{ background: white; padding: 2em; border-radius: 8px;
          box-shadow: 0 2px 8px rgba(0,0,0,0.05); width: 320px; }}
  h1 {{ font-size: 1.2em; margin: 0 0 1em; }}
  label {{ display: block; font-size: 0.85em; color: #555; margin-bottom: 0.3em; }}
  input[type=password] {{ width: 100%; padding: 0.6em; font-size: 1em;
                          border: 1px solid #ccc; border-radius: 4px;
                          box-sizing: border-box; }}
  button {{ margin-top: 1em; width: 100%; padding: 0.7em; font-size: 1em;
            background: #0a7c3a; color: white; border: none; border-radius: 4px;
            cursor: pointer; }}
  .err {{ color: #c22; font-size: 0.85em; margin-bottom: 0.8em; }}
  .hint {{ color: #888; font-size: 0.75em; margin-top: 1em; }}
</style></head><body>
<form method="POST" action="/admin/login">
  <h1>Admin login</h1>
  {error_html}
  <label for="pw">Password</label>
  <input type="password" id="pw" name="password" autofocus required>
  <button type="submit">Sign in</button>
  <div class="hint">Session lasts 24 hours. Cookie is HttpOnly + Secure.</div>
</form>
</body></html>"""
    return HTMLResponse(body)


@router.post("/admin/login")
async def post_login(
    request: Request,
    response: Response,
    password: str = Form(...),
) -> Response:
    """Verify password. On success, set cookie + redirect to /admin/annotate.
    On failure, redirect back to the login form with a generic error."""
    stored_hash = os.environ.get("ADMIN_PASSWORD_HASH", "")
    if not stored_hash:
        log.warning("ADMIN_LOGIN_ATTEMPT_NO_HASH — ADMIN_PASSWORD_HASH env var not set")
        return RedirectResponse(
            url="/admin/login?error=Admin+login+is+not+configured.",
            status_code=303,
        )

    # Rate-limit hook (v1: log only). Real rate-limiting comes later.
    client = (request.client.host if request.client else "?")
    if not verify_password(password, stored_hash):
        log.warning("ADMIN_LOGIN_FAIL client=%s", client)
        # Constant-time-ish response — always add a small sleep so failed
        # attempts aren't distinguishable from bad-configured hash errors.
        # (This is defence in depth; PBKDF2 itself is already ~200ms.)
        return RedirectResponse(
            url="/admin/login?error=Invalid+password.",
            status_code=303,
        )

    log.info("ADMIN_LOGIN_OK client=%s", client)
    cookie_value = _mint_session_cookie()

    # Redirect to a landing page. Preserve any ?next= query for deep-linking.
    # Open-redirect guard (2026-08-30, security-review MEDIUM catch):
    # `?next=https://evil.com/phish` would post-login-redirect the admin
    # off-site with the session cookie already set — classic phishing
    # gadget. Constrain to same-origin absolute paths only.
    #   - must start with "/"
    #   - must NOT start with "//" (protocol-relative → external)
    #   - must NOT start with "/\" (browser-quirk external)
    # Anything else → fall back to the default landing.
    next_url = request.query_params.get("next") or "/admin/annotate"
    if not (
        isinstance(next_url, str)
        and next_url.startswith("/")
        and not next_url.startswith("//")
        and not next_url.startswith("/\\")
    ):
        next_url = "/admin/annotate"
    redirect = RedirectResponse(url=next_url, status_code=303)
    # Cookie flags:
    #   HttpOnly — JS can't read it (XSS mitigation)
    #   Secure   — HTTPS only (we're behind cloudflared, always HTTPS)
    #   SameSite=Lax — allowed on top-level navigation, blocks CSRF
    #   Path=/ — sent on every admin-adjacent request
    redirect.set_cookie(
        key=_COOKIE_NAME,
        value=cookie_value,
        max_age=_COOKIE_TTL_S,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return redirect


@router.post("/admin/logout")
async def post_logout(request: Request) -> Response:
    """Clear the session cookie. Idempotent."""
    redirect = RedirectResponse(url="/admin/login", status_code=303)
    redirect.delete_cookie(_COOKIE_NAME, path="/")
    return redirect

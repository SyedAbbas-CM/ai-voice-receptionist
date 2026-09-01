"""Admin UI for per-tenant integration configuration.

Routes:
  * GET  /admin/tenants                       — list every business.json
  * GET  /admin/tenants/{slug}                — detail (integrations summary)
  * GET  /admin/tenants/{slug}/integrations   — edit form
  * POST /admin/tenants/{slug}/integrations   — save form → writes back
  * POST /admin/tenants/{slug}/test/{backend} — live test each backend's creds

`slug` is the sample-data directory name (e.g. "clinic", "restaurant").
We write back to `sample-data/<slug>/business.json` — same path
session_manager reads from.

Auth: reuses `_require_admin` from routes/admin (same as annotator +
recordings). No tenant Bearer path — this is an operator function.

Design decisions:
  - Editorial palette matches the annotator (vermilion + warm paper).
  - Password / token fields render as `type="password"` and never
    round-trip the stored value in the raw HTML — instead show a
    placeholder like `pit-...set` if the field has a value, and let
    the operator overwrite or clear.  Prevents accidental disclosure
    of secrets on someone shoulder-surfing the browser.
  - Save = full replace of `integrations` block. Existing
    non-integrations fields (name, hours, services) are preserved
    verbatim — we round-trip via BusinessProfile.model_dump.
  - After save, the cache in session_manager is BLASTED so the next
    call picks up the new config without a service restart.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from packages.schemas.business import BusinessProfile, Integrations
from app.routes.admin import _require_admin


log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/tenants", tags=["admin", "tenants"])


_REPO_ROOT = Path(__file__).resolve().parents[4]
_TENANTS_DIR = _REPO_ROOT / "sample-data"
_SECRET_FIELDS = {
    "ghl_api_token",
    "hubspot_access_token",
    "google_service_account_json",
    "webhook_hmac_secret",
}


def _list_tenants() -> list[dict[str, Any]]:
    """Enumerate every sample-data/*/business.json we can find."""
    out = []
    if not _TENANTS_DIR.exists():
        return out
    for path in sorted(_TENANTS_DIR.iterdir()):
        biz_file = path / "business.json"
        if not biz_file.is_file():
            continue
        try:
            raw = json.loads(biz_file.read_text())
            biz = BusinessProfile(**raw)
            out.append({
                "slug": path.name,
                "id": biz.id,
                "name": biz.name,
                "vertical": biz.vertical,
                "backend": biz.integrations.calendar_backend,
                "sinks": biz.integrations.crm_sinks or [],
                "path": str(biz_file),
            })
        except Exception as e:
            log.warning("skipping malformed tenant %s: %s", path.name, e)
    return out


def _load_tenant(slug: str) -> tuple[BusinessProfile, dict, Path]:
    """Load the raw dict + parsed BusinessProfile + on-disk path."""
    if not re.match(r"^[a-zA-Z0-9_-]+$", slug):
        raise HTTPException(400, "invalid tenant slug")
    biz_file = _TENANTS_DIR / slug / "business.json"
    if not biz_file.is_file():
        raise HTTPException(404, f"no business.json for tenant {slug!r}")
    try:
        raw = json.loads(biz_file.read_text())
        biz = BusinessProfile(**raw)
    except Exception as e:
        raise HTTPException(500, f"failed to parse {slug} business.json: {e}")
    return biz, raw, biz_file


def _redact_secret(value: Optional[str], kind: str = "generic") -> str:
    """Return a placeholder like 'pit-…set' when a secret is present.
    Never leaks the actual value into HTML."""
    if not value:
        return ""
    if kind == "ghl_token" and value.startswith("pit-"):
        return "pit-…set"
    return "…set"


def _require_same_origin(request: Request) -> None:
    """2026-09-01 security-review fix: defence-in-depth CSRF guard.

    Admin session cookie is SameSite=Lax which alone blocks the
    common cross-site POST exploit vectors. This adds Origin/Referer
    validation as a second layer: reject any POST whose Origin
    (or Referer if Origin missing) doesn't match the request's own
    host. Blocks the residual attack surface (subdomain takeovers,
    malicious pages hosted on same eTLD+1 where SameSite=Lax
    doesn't strictly apply, HTTP/2 push shenanigans).

    Cheaper than a token flow AND catches the class the auditor
    flagged.
    """
    origin = request.headers.get("origin") or ""
    referer = request.headers.get("referer") or ""
    # Reconstruct our own scheme://host from the incoming request
    # (respect X-Forwarded-Proto since we're behind nginx on Lightsail).
    fwd_proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    fwd_host = request.headers.get("host") or request.url.hostname or ""
    our_origin = f"{fwd_proto}://{fwd_host}"

    check_source = origin if origin else referer
    if not check_source:
        # No Origin AND no Referer — some ancient clients don't send
        # either, but modern browsers always do. Refuse for admin
        # POSTs; safer than allowing an unmarked request.
        raise HTTPException(
            403,
            "CSRF check failed: request has neither Origin nor Referer header",
        )

    try:
        parsed = urlparse(check_source)
        source_origin = f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        raise HTTPException(403, "CSRF check failed: unparseable Origin/Referer")

    if source_origin != our_origin:
        # Common case: request came from the app itself (e.g. redirect
        # after login). Log at INFO not warn — real attacks are rare
        # and the log helps diagnose legitimate cross-origin (dev
        # tunneling, etc.) as much as attacks.
        log.info(
            "CSRF_REJECT admin-tenants source_origin=%r our_origin=%r path=%s",
            source_origin, our_origin, request.url.path,
        )
        raise HTTPException(
            403,
            f"CSRF check failed: request origin {source_origin!r} does "
            f"not match server origin {our_origin!r}",
        )


def _invalidate_caches() -> None:
    """Drop session_manager's cached calendar + sink so the next call
    picks up the new config. Called after every successful save."""
    try:
        from app.core import session_manager as _sm
        _sm._sink_cache = None
        _sm._calendar_cache = None
        _sm._business_cache = None
        log.info("admin/tenants: invalidated session_manager caches after save")
    except Exception:
        log.warning("cache invalidation failed", exc_info=True)


# ─── GET /admin/tenants — list ─────────────────────────────────────────


@router.get("", response_class=HTMLResponse)
def get_index(request: Request) -> HTMLResponse:
    _require_admin(request)
    tenants = _list_tenants()

    rows = []
    for t in tenants:
        sinks = ", ".join(t["sinks"]) if t["sinks"] else "—"
        rows.append(
            f'<tr onclick="location.href=\'/admin/tenants/{html.escape(t["slug"])}/integrations\'" style="cursor:pointer">'
            f'<td class="name">{html.escape(t["name"])}</td>'
            f'<td class="slug"><code>{html.escape(t["slug"])}</code></td>'
            f'<td class="vertical">{html.escape(t["vertical"])}</td>'
            f'<td class="backend"><span class="pill">{html.escape(t["backend"])}</span></td>'
            f'<td class="sinks">{html.escape(sinks)}</td>'
            f'<td class="actions"><a href="/admin/tenants/{html.escape(t["slug"])}/integrations">edit →</a></td>'
            f'</tr>'
        )

    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Tenants</title>
<style>{_TENANTS_CSS}</style></head><body><div class="wrap">
<header class="mast">
  <div><span class="kicker">Operator console</span><h1>Tenants</h1></div>
  <div class="meta"><strong>{len(tenants)}</strong> tenants configured<br><a href="/admin/annotate">← annotate</a></div>
</header>
<p class="section-title">Every tenant registered on this instance</p>
<table>
<thead><tr>
  <th>Name</th><th>Slug</th><th>Vertical</th>
  <th>Calendar</th><th>CRM sinks</th><th></th>
</tr></thead>
<tbody>{"".join(rows) if rows else '<tr><td colspan="6" class="empty">No tenants yet. Add one under sample-data/&lt;slug&gt;/business.json.</td></tr>'}</tbody>
</table>
<div class="colophon"><span>VoiceOps · <strong>tenants</strong></span></div>
</div></body></html>""")


# ─── GET /admin/tenants/{slug}/integrations — edit form ─────────────────


@router.get("/{slug}/integrations", response_class=HTMLResponse)
def get_integrations_form(slug: str, request: Request) -> HTMLResponse:
    _require_admin(request)
    biz, _raw, _path = _load_tenant(slug)
    integ = biz.integrations

    def _checked(v, opt): return "checked" if v == opt else ""
    def _sink_checked(sink): return "checked" if sink in (integ.crm_sinks or []) else ""

    # Redact secrets in placeholders so the current-set-value is not
    # visible in the rendered HTML source.
    ghl_token_ph = _redact_secret(integ.ghl_api_token, "ghl_token")
    hs_token_ph = _redact_secret(integ.hubspot_access_token)
    google_json_ph = _redact_secret(integ.google_service_account_json)
    wh_secret_ph = _redact_secret(integ.webhook_hmac_secret)

    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{html.escape(biz.name)} · integrations</title>
<style>{_TENANTS_CSS}{_FORM_CSS}</style></head><body><div class="wrap">
<header class="mast">
  <div>
    <span class="kicker"><a href="/admin/tenants">← all tenants</a></span>
    <h1>{html.escape(biz.name)}</h1>
    <div class="submeta"><code>{html.escape(slug)}</code> · {html.escape(biz.vertical)}</div>
  </div>
  <div class="meta">
    Editing <strong>integrations</strong> config<br>
    Saves to <code>sample-data/{html.escape(slug)}/business.json</code>
  </div>
</header>

<form method="POST" action="/admin/tenants/{html.escape(slug)}/integrations" id="config">

  <section class="card">
    <h2>Calendar backend</h2>
    <p class="hint">Where the agent reads availability AND writes bookings.</p>
    <div class="radio-row">
      <label><input type="radio" name="calendar_backend" value="fake" {_checked(integ.calendar_backend, "fake")}> <span>Local (fake)</span></label>
      <label><input type="radio" name="calendar_backend" value="google" {_checked(integ.calendar_backend, "google")}> <span>Google Calendar</span></label>
      <label><input type="radio" name="calendar_backend" value="ghl" {_checked(integ.calendar_backend, "ghl")}> <span>GoHighLevel</span></label>
    </div>
  </section>

  <section class="card">
    <h2>CRM sinks</h2>
    <p class="hint">Every checked sink fires on every booking. Composite.</p>
    <div class="check-row">
      <label><input type="checkbox" name="sink_ghl" {_sink_checked("ghl")}> <span>GoHighLevel</span></label>
      <label><input type="checkbox" name="sink_hubspot" {_sink_checked("hubspot")}> <span>HubSpot</span></label>
      <label><input type="checkbox" name="sink_webhook" {_sink_checked("webhook")}> <span>Webhook (n8n / Make / Zapier)</span></label>
    </div>
  </section>

  <section class="card">
    <h2>GoHighLevel</h2>
    <p class="hint">Needed when Calendar = GHL or Sinks includes GHL. Use a <strong>Location-scoped</strong> Private Integration token — Agency-level tokens don't grant contacts.write.</p>
    <div class="field-grid">
      <label>API token (PIT)
        <input type="password" name="ghl_api_token" placeholder="{ghl_token_ph or 'pit-...'}" autocomplete="new-password">
        <span class="hint-sm">Leave blank to keep existing. Type "clear" to wipe.</span>
      </label>
      <label>Location ID
        <input type="text" name="ghl_location_id" value="{html.escape(integ.ghl_location_id or '')}" placeholder="e.g. NGw3mS9kVaiQoFIOTiAz">
      </label>
      <label>Calendar ID (required for GHL calendar backend)
        <input type="text" name="ghl_calendar_id" value="{html.escape(integ.ghl_calendar_id or '')}" placeholder="Settings → Calendars → copy from URL">
      </label>
    </div>
    <button type="button" class="test-btn" data-backend="ghl">Test GHL connection</button>
    <div class="test-result" id="test-ghl"></div>
  </section>

  <section class="card">
    <h2>HubSpot</h2>
    <p class="hint">Needed when Sinks includes HubSpot. Get a Private App token in HubSpot → Settings → Integrations → Private Apps.</p>
    <div class="field-grid">
      <label>Access token
        <input type="password" name="hubspot_access_token" placeholder="{hs_token_ph or 'pat-na1-...'}" autocomplete="new-password">
      </label>
      <label>Portal ID (optional)
        <input type="text" name="hubspot_portal_id" value="{html.escape(integ.hubspot_portal_id or '')}">
      </label>
      <label>Pipeline ID (optional, for deals)
        <input type="text" name="hubspot_pipeline_id" value="{html.escape(integ.hubspot_pipeline_id or '')}">
      </label>
      <label>Stage ID (optional, for deals)
        <input type="text" name="hubspot_stage_id" value="{html.escape(integ.hubspot_stage_id or '')}">
      </label>
      <label class="checkbox-inline">
        <input type="checkbox" name="hubspot_create_deals" {"checked" if integ.hubspot_create_deals else ""}> Create deals (not just contacts)
      </label>
    </div>
    <button type="button" class="test-btn" data-backend="hubspot">Test HubSpot connection</button>
    <div class="test-result" id="test-hubspot"></div>
  </section>

  <section class="card">
    <h2>Google Calendar</h2>
    <p class="hint">Needed when Calendar = Google. Provide a service-account JSON path AND the calendar ID.</p>
    <div class="field-grid">
      <label>Service account JSON (file path)
        <input type="password" name="google_service_account_json" placeholder="{google_json_ph or '/etc/voiceops/gcp-sa.json'}" autocomplete="new-password">
      </label>
      <label>Calendar ID
        <input type="text" name="google_calendar_id" value="{html.escape(integ.google_calendar_id or '')}" placeholder="primary@group.calendar.google.com">
      </label>
    </div>
  </section>

  <section class="card">
    <h2>Webhook (n8n / Make / Zapier)</h2>
    <p class="hint">Needed when Sinks includes Webhook. We POST call events (booked, missed, escalated) to this URL, signed with HMAC-SHA256.</p>
    <div class="field-grid">
      <label>Webhook URL
        <input type="url" name="webhook_url" value="{html.escape(integ.webhook_url or '')}" placeholder="https://n8n.example/hook/abc">
      </label>
      <label>HMAC secret
        <input type="password" name="webhook_hmac_secret" placeholder="{wh_secret_ph or '32+ char shared secret'}" autocomplete="new-password">
      </label>
    </div>
  </section>

  <div class="actions">
    <button type="submit" class="save">Save changes</button>
    <a href="/admin/tenants" class="cancel">Cancel</a>
  </div>
</form>

<script>
document.querySelectorAll('.test-btn').forEach(btn => {{
  btn.addEventListener('click', async () => {{
    const backend = btn.dataset.backend;
    const target = document.getElementById('test-' + backend);
    target.textContent = 'Testing…';
    target.className = 'test-result testing';
    try {{
      const form = document.getElementById('config');
      const data = new FormData(form);
      const resp = await fetch('/admin/tenants/{html.escape(slug)}/test/' + backend, {{
        method: 'POST', body: data, credentials: 'include',
      }});
      const json = await resp.json();
      if (json.ok) {{
        target.textContent = '✓ ' + json.detail;
        target.className = 'test-result ok';
      }} else {{
        target.textContent = '✗ ' + (json.error || 'test failed');
        target.className = 'test-result fail';
      }}
    }} catch (e) {{
      target.textContent = '✗ ' + e.message;
      target.className = 'test-result fail';
    }}
  }});
}});
</script>
<div class="colophon"><span>VoiceOps · <strong>tenant config</strong></span></div>
</div></body></html>""")


# ─── POST /admin/tenants/{slug}/integrations — save ─────────────────────


@router.post("/{slug}/integrations")
async def save_integrations(slug: str, request: Request):
    _require_admin(request)
    _require_same_origin(request)
    biz, raw, path = _load_tenant(slug)
    form = await request.form()

    # Build new Integrations from form; keep existing secret when
    # field is blank (allows "edit without touching password").
    def _keep_secret(field_key: str, form_key: str) -> Optional[str]:
        submitted = (form.get(form_key) or "").strip()
        if submitted.lower() == "clear":
            return None
        if not submitted:
            return getattr(biz.integrations, field_key)
        return submitted

    sinks = []
    if form.get("sink_ghl"): sinks.append("ghl")
    if form.get("sink_hubspot"): sinks.append("hubspot")
    if form.get("sink_webhook"): sinks.append("webhook")

    new_integ = Integrations(
        calendar_backend=(form.get("calendar_backend") or "fake").strip() or "fake",
        crm_sinks=sinks,
        ghl_api_token=_keep_secret("ghl_api_token", "ghl_api_token"),
        ghl_location_id=(form.get("ghl_location_id") or "").strip() or None,
        ghl_calendar_id=(form.get("ghl_calendar_id") or "").strip() or None,
        hubspot_access_token=_keep_secret("hubspot_access_token", "hubspot_access_token"),
        hubspot_portal_id=(form.get("hubspot_portal_id") or "").strip() or None,
        hubspot_pipeline_id=(form.get("hubspot_pipeline_id") or "").strip() or None,
        hubspot_stage_id=(form.get("hubspot_stage_id") or "").strip() or None,
        hubspot_create_deals=bool(form.get("hubspot_create_deals")),
        google_service_account_json=_keep_secret(
            "google_service_account_json", "google_service_account_json",
        ),
        google_calendar_id=(form.get("google_calendar_id") or "").strip() or None,
        webhook_url=(form.get("webhook_url") or "").strip() or None,
        webhook_hmac_secret=_keep_secret(
            "webhook_hmac_secret", "webhook_hmac_secret",
        ),
    )

    # Preserve every other field on the business — only rewrite
    # `integrations`. Round-trip through model_dump so we honour
    # pydantic defaults + validation.
    new_biz_dict = dict(raw)
    new_biz_dict["integrations"] = new_integ.model_dump()
    # Validate the composed profile before write
    try:
        BusinessProfile(**new_biz_dict)
    except Exception as e:
        raise HTTPException(400, f"validation failed: {e}")

    # 2026-09-01 security-review fix: business.json contains secrets
    # (GHL PIT, HubSpot PAT, Google SA JSON path, webhook HMAC).
    # Write with owner-only permissions (0o600) and lock the parent
    # dir to 0o700 so other users on the box can't read the file
    # even between the temp-write and the atomic rename.
    tmp_path = path.with_suffix(".json.tmp")
    payload = json.dumps(new_biz_dict, indent=2)
    # Create with restrictive mode from the start — never write with
    # umask-default mode and chmod after.
    fd = os.open(
        str(tmp_path),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(payload)
    except Exception:
        # If we opened but write failed, best-effort remove the
        # partial temp so a rerun doesn't hit a truncated file
        try:
            tmp_path.unlink()
        except Exception:
            pass
        raise
    tmp_path.replace(path)
    # Explicitly re-apply after rename (replace() preserves mode on
    # POSIX but be paranoid on non-POSIX filesystems).
    try:
        os.chmod(path, 0o600)
        os.chmod(path.parent, 0o700)
    except Exception:
        log.warning("chmod tighten failed for %s (may be non-POSIX FS)", path)

    _invalidate_caches()

    return RedirectResponse(
        url=f"/admin/tenants/{slug}/integrations?saved=1",
        status_code=303,
    )


# ─── POST /admin/tenants/{slug}/test/{backend} — live connection test ──


@router.post("/{slug}/test/{backend}")
async def test_backend(
    slug: str, backend: str, request: Request,
) -> JSONResponse:
    _require_admin(request)
    _require_same_origin(request)
    biz, _raw, _path = _load_tenant(slug)
    form = await request.form()

    if backend == "ghl":
        token = (form.get("ghl_api_token") or "").strip() or biz.integrations.ghl_api_token
        location = (form.get("ghl_location_id") or "").strip() or biz.integrations.ghl_location_id
        if not token or not location:
            return JSONResponse({"ok": False, "error": "token + location required"})
        try:
            from packages.integrations.ghl_client import GoHighLevelClient
            client = GoHighLevelClient(api_token=token, location_id=location)
            # Cheap GET that requires location-scoped perms
            data = await client._request("GET", f"/locations/{location}")
            name = (data.get("location") or {}).get("name") or "(no name)"
            return JSONResponse({"ok": True, "detail": f"Connected to GHL location: {name}"})
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)[:200]})

    if backend == "hubspot":
        token = (form.get("hubspot_access_token") or "").strip() or biz.integrations.hubspot_access_token
        if not token:
            return JSONResponse({"ok": False, "error": "access token required"})
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as c:
                resp = await c.get(
                    "https://api.hubapi.com/account-info/v3/details",
                    headers={"Authorization": f"Bearer {token}"},
                )
            if resp.status_code >= 400:
                return JSONResponse({"ok": False, "error": f"HubSpot HTTP {resp.status_code}: {resp.text[:200]}"})
            d = resp.json()
            return JSONResponse({"ok": True, "detail": f"Connected to HubSpot portal: {d.get('portalId') or d.get('hub_id') or 'ok'}"})
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)[:200]})

    return JSONResponse({"ok": False, "error": f"unknown backend {backend!r}"}, status_code=400)


# ─── CSS ────────────────────────────────────────────────────────────────

_TENANTS_CSS = """
:root {
  --paper: #f7f5ef; --ink: #1a1a1a; --ink-2: #4a4a4a; --ink-3: #7a7a7a;
  --rule: #d9d3c4; --card: #ffffff; --card-2: #fbf9f2;
  --accent: #b8360f; --accent-soft: #f2ddd4; --good: #2f6b2a; --bad: #a11b1b;
  --hover: #f2ecd9;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --paper: #14140f; --ink: #f0ece0; --ink-2: #b8b3a3; --ink-3: #7a7568;
    --rule: #2f2c22; --card: #1c1a15; --card-2: #17150f;
    --accent: #e46540; --accent-soft: #3a1f14; --good: #6ec665; --bad: #e26a6a;
    --hover: #22201a;
  }
}
html { color-scheme: light dark; }
* { box-sizing: border-box; }
body { background: var(--paper); color: var(--ink);
  font-family: "Charter", "Iowan Old Style", "Georgia", serif;
  font-size: 15px; line-height: 1.5; margin: 0; padding: 40px 32px; }
.wrap { max-width: 960px; margin: 0 auto; }
header.mast { border-bottom: 3px double var(--ink); padding-bottom: 16px;
  margin-bottom: 24px; display: flex; align-items: baseline;
  justify-content: space-between; gap: 16px; flex-wrap: wrap; }
header.mast h1 { font-family: "Playfair Display", "Didot", serif;
  font-size: 30px; font-weight: 900; margin: 0; line-height: 1; }
header.mast .kicker { font-family: "SF Mono", monospace; font-size: 10px;
  letter-spacing: 0.22em; text-transform: uppercase; color: var(--ink-3);
  display: block; margin-bottom: 6px; }
header.mast .kicker a { color: inherit; text-decoration: none; }
header.mast .kicker a:hover { color: var(--accent); }
header.mast .meta { font-family: "SF Mono", monospace; font-size: 10px;
  letter-spacing: 0.14em; text-transform: uppercase; color: var(--ink-2);
  text-align: right; line-height: 1.7; }
header.mast .meta strong { color: var(--accent); }
.submeta { color: var(--ink-3); font-size: 12px; margin-top: 4px; }
.submeta code { font-family: "SF Mono", monospace; font-size: 11px; }
.section-title { font-family: "SF Mono", monospace; font-size: 10px;
  letter-spacing: 0.22em; text-transform: uppercase; color: var(--ink-3);
  margin: 0 0 10px; padding-bottom: 8px; border-bottom: 1px solid var(--rule); }
table { border-collapse: collapse; width: 100%; background: var(--card);
  border: 1px solid var(--rule); }
thead th { text-align: left; padding: 12px 16px; background: var(--card-2);
  border-bottom: 1px solid var(--rule); font-family: "SF Mono", monospace;
  font-size: 10px; letter-spacing: 0.16em; text-transform: uppercase;
  color: var(--ink-3); font-weight: 600; }
tbody td { padding: 12px 16px; border-bottom: 1px solid var(--rule); font-size: 14px; }
tbody tr:hover { background: var(--hover); }
td.name { font-weight: 600; }
td.slug code, td.vertical { font-family: "SF Mono", monospace; font-size: 12px; color: var(--ink-2); }
.pill { display: inline-block; padding: 2px 10px; border-radius: 12px;
  font-family: "SF Mono", monospace; font-size: 10px; letter-spacing: 0.12em;
  text-transform: uppercase; font-weight: 700; background: var(--accent-soft);
  color: var(--accent); }
td.sinks { font-family: "SF Mono", monospace; font-size: 12px; color: var(--ink-2); }
td.actions { text-align: right; }
td.actions a { color: var(--ink); text-decoration: none; font-family: "SF Mono", monospace;
  font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase; font-weight: 600; }
td.actions a:hover { color: var(--accent); }
.empty { padding: 40px 20px; text-align: center; color: var(--ink-3); font-style: italic; }
.colophon { margin-top: 48px; padding-top: 16px; border-top: 3px double var(--ink);
  font-family: "SF Mono", monospace; font-size: 10px; letter-spacing: 0.14em;
  text-transform: uppercase; color: var(--ink-3); display: flex;
  justify-content: space-between; flex-wrap: wrap; gap: 12px; }
.colophon strong { color: var(--accent); }
"""

_FORM_CSS = """
form#config { margin-top: 8px; }
.card { background: var(--card); border: 1px solid var(--rule); padding: 20px 24px;
  margin-bottom: 20px; border-radius: 2px; }
.card h2 { font-family: "Playfair Display", "Didot", serif; font-size: 20px;
  margin: 0 0 6px; font-weight: 700; }
.card .hint { color: var(--ink-2); font-size: 13px; margin: 0 0 16px; }
.hint strong { color: var(--accent); }
.hint-sm { font-size: 11px; color: var(--ink-3); font-family: "SF Mono", monospace;
  letter-spacing: 0.05em; }
.radio-row, .check-row { display: flex; gap: 20px; flex-wrap: wrap; }
.radio-row label, .check-row label { display: flex; align-items: center; gap: 8px;
  cursor: pointer; font-size: 15px; padding: 10px 16px; border: 1px solid var(--rule);
  border-radius: 2px; background: var(--card-2); }
.radio-row label:has(input:checked), .check-row label:has(input:checked) {
  border-color: var(--accent); background: var(--accent-soft);
}
.field-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px 20px; }
.field-grid label { display: flex; flex-direction: column; gap: 4px; font-size: 12px;
  color: var(--ink-2); font-family: "SF Mono", monospace; letter-spacing: 0.06em;
  text-transform: uppercase; font-weight: 600; }
.field-grid input { padding: 8px 12px; border: 1px solid var(--rule); border-radius: 2px;
  background: var(--card-2); color: var(--ink); font-size: 14px; font-family: inherit;
  font-weight: 400; letter-spacing: normal; text-transform: none; }
.field-grid input:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
.checkbox-inline { grid-column: 1 / -1; flex-direction: row !important;
  text-transform: none !important; letter-spacing: normal !important;
  font-family: inherit !important; font-size: 14px !important; color: var(--ink) !important; }
.test-btn { margin-top: 16px; padding: 8px 16px; background: var(--card-2);
  border: 1px solid var(--rule); border-radius: 2px; cursor: pointer;
  font-family: "SF Mono", monospace; font-size: 11px; letter-spacing: 0.1em;
  text-transform: uppercase; color: var(--ink); font-weight: 600; }
.test-btn:hover { border-color: var(--accent); color: var(--accent); }
.test-result { margin-top: 10px; font-size: 13px; padding: 8px 12px; border-radius: 2px; }
.test-result.testing { color: var(--ink-3); }
.test-result.ok { color: var(--good); background: rgba(47, 107, 42, 0.1); }
.test-result.fail { color: var(--bad); background: rgba(161, 27, 27, 0.1); font-family: "SF Mono", monospace; font-size: 12px; }
.actions { display: flex; gap: 16px; align-items: center; margin: 24px 0; }
.actions button.save { padding: 10px 24px; background: var(--accent); color: white;
  border: none; border-radius: 2px; cursor: pointer; font-family: "SF Mono", monospace;
  font-size: 12px; letter-spacing: 0.14em; text-transform: uppercase; font-weight: 700; }
.actions button.save:hover { background: var(--ink); }
.actions .cancel { color: var(--ink-3); text-decoration: none; font-size: 13px; }
.actions .cancel:hover { color: var(--accent); }
@media (max-width: 640px) {
  .field-grid { grid-template-columns: 1fr; }
  .radio-row, .check-row { flex-direction: column; gap: 8px; }
}
"""

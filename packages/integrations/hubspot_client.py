"""HubSpot CRM v3 API client.

Auth: HubSpot Private App access token. The owner of the HubSpot account
generates one under Settings → Integrations → Private Apps, grants the
scopes we need (crm.objects.contacts.read/write, crm.objects.notes.write,
crm.objects.deals.write, timeline), and pastes the token into env as
`HUBSPOT_ACCESS_TOKEN`. No OAuth flow needed — mirrors how GHL Private
Integrations work.

Docs:
  - Contacts:    https://developers.hubspot.com/docs/api/crm/contacts
  - Notes/Engagements: https://developers.hubspot.com/docs/api/crm/notes
  - Search:      https://developers.hubspot.com/docs/api/crm/search

Free-tier quota (2026):
  - 100 requests/10s burst, 250k/day for Free/Starter accounts
  - Sufficient for well under 1k booked calls/day at ~3 API calls/booking

Design notes:
  - Symmetric with `ghl_client.GoHighLevelClient` — same public method
    shapes (upsert_contact / add_note / etc.) so `HubSpotSink` in
    `sinks.py` can look like `GHLSink` and swap freely.
  - HubSpot doesn't have a native "upsert by phone" endpoint on the
    Contact object.  We implement it as: search by phone → PATCH if
    found, POST if not.  Two API calls in the new-contact case; one in
    the repeat-caller case.
  - Notes on HubSpot are Engagements with type=NOTE.  Slightly heavier
    payload than GHL's `/contacts/{id}/notes` but same effect.
  - Deals are HubSpot's opportunity equivalent.  Optional — off by
    default; enable via `HUBSPOT_CREATE_DEALS=true` env once a
    tenant's pipeline_id + stage_id are configured.
"""
from __future__ import annotations

import time
from typing import Optional

import httpx


HUBSPOT_BASE = "https://api.hubapi.com"


class HubSpotError(Exception):
    """Raised when a HubSpot API call fails.  Sink callers swallow and log."""


class HubSpotClient:
    def __init__(
        self,
        access_token: str,
        portal_id: Optional[str] = None,
        default_pipeline_id: Optional[str] = None,
        default_stage_id: Optional[str] = None,
        create_deals: bool = False,
        timeout: float = 15.0,
    ) -> None:
        if not access_token:
            raise HubSpotError("HUBSPOT_ACCESS_TOKEN not set")
        self.access_token = access_token
        # portal_id is for building URLs in notes so a tenant clicking
        # a link lands on their portal directly.  Not required for API
        # calls themselves.
        self.portal_id = portal_id
        self.default_pipeline_id = default_pipeline_id
        self.default_stage_id = default_stage_id
        self.create_deals = create_deals
        self.timeout = timeout

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # 2026-08-25 (ChatGPT audit P1): retry policy for transient failures.
    #
    # HubSpot free tier: 100 req/10s burst, 250k/day.  Search endpoints
    # have STRICTER per-second limits.  Under real load a booking-flow
    # sequence (search → upsert → note → deal = 4 requests) can hit 429
    # on the search endpoint even at low tenant scale.
    #
    # Policy per audit recommendation:
    # - RETRYABLE:   408 (request timeout), 429 (rate limit),
    #                500/502/503/504 (upstream transient), network errors
    # - NOT RETRYABLE: 400 (validation), 401/403 (auth), 404 (signal)
    # - Honor Retry-After when HubSpot sends it; else jittered exp backoff
    # - Max 4 attempts (initial + 3 retries) — beyond that, outbox path
    #   takes over (once networking's #P1 outbox lands).
    _MAX_ATTEMPTS = 4
    _RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
    # Base backoff 250ms, doubling each try, with ±30% jitter.
    _BACKOFF_BASE_S = 0.25
    _BACKOFF_MAX_S = 8.0

    @staticmethod
    def _next_backoff(attempt: int, retry_after: Optional[str]) -> float:
        """Compute sleep time before next retry.

        HubSpot's Retry-After header is either delta-seconds ("30") or
        an HTTP-date.  We honor delta-seconds and fall back to
        exponential backoff for anything else — parsing HTTP-date only
        to eat variance for what's almost always seconds anyway.
        """
        if retry_after:
            try:
                seconds = float(retry_after.strip())
                if 0 < seconds < HubSpotClient._BACKOFF_MAX_S * 4:
                    return seconds
            except (TypeError, ValueError):
                pass
        base = HubSpotClient._BACKOFF_BASE_S * (2 ** (attempt - 1))
        # Deterministic jitter (attempt-based) — avoids Date.now()/random()
        # in this async path (keeps test replayability + workflow rules).
        jitter = 0.7 + 0.3 * ((attempt * 17) % 10) / 10.0
        return min(base * jitter, HubSpotClient._BACKOFF_MAX_S)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> dict:
        import asyncio
        last_exc: Optional[Exception] = None
        for attempt in range(1, self._MAX_ATTEMPTS + 1):
            resp = None
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.request(
                        method,
                        f"{HUBSPOT_BASE}{path}",
                        headers=self._headers,
                        json=json,
                        params=params,
                    )
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                # Network-layer failures are retryable.
                last_exc = e
                if attempt < self._MAX_ATTEMPTS:
                    await asyncio.sleep(self._next_backoff(attempt, None))
                    continue
                raise HubSpotError(
                    f"HubSpot {method} {path} network error after "
                    f"{attempt} attempts: {e}"
                ) from e

            if resp.status_code == 404:
                # 404 is a normal signal on search-miss / not-found lookups.
                return {"_not_found": True}
            if resp.status_code < 400:
                return resp.json() if resp.content else {}

            # 400/401/403 are NOT retryable — validation / auth failures
            # will always fail the same way, retrying just wastes quota.
            if resp.status_code not in self._RETRYABLE_STATUS:
                raise HubSpotError(
                    f"HubSpot {method} {path} -> {resp.status_code}: "
                    f"{resp.text[:400]}"
                )
            # Retryable — sleep + loop (unless we're at the last attempt).
            if attempt < self._MAX_ATTEMPTS:
                await asyncio.sleep(self._next_backoff(
                    attempt, resp.headers.get("Retry-After"),
                ))
                continue
            # Exhausted attempts on a retryable status — raise so the
            # sink swallows + logs (and later the outbox retries later).
            raise HubSpotError(
                f"HubSpot {method} {path} -> {resp.status_code} after "
                f"{attempt} attempts (retryable): {resp.text[:400]}"
            )
        # Unreachable — loop always returns or raises.  Defensive.
        assert last_exc is not None
        raise HubSpotError(str(last_exc))

    # ── contacts ─────────────────────────────────────────────────────

    async def find_contact_by_phone(self, phone: str) -> Optional[dict]:
        """Search for a contact by exact phone match.  Returns the
        first result or None.

        HubSpot's `phone` property stores whatever format the tenant
        typed originally.  We search both `phone` and `mobilephone`
        properties since real users mix them.
        """
        if not phone:
            return None
        # Search API supports OR at the top-level via multiple
        # filterGroups.  Each filterGroup is AND internally; groups
        # are OR'd together.
        payload = {
            "filterGroups": [
                {"filters": [{
                    "propertyName": "phone",
                    "operator": "EQ",
                    "value": phone,
                }]},
                {"filters": [{
                    "propertyName": "mobilephone",
                    "operator": "EQ",
                    "value": phone,
                }]},
            ],
            "properties": ["firstname", "lastname", "email", "phone", "mobilephone"],
            "limit": 1,
        }
        data = await self._request(
            "POST", "/crm/v3/objects/contacts/search", json=payload,
        )
        if data.get("_not_found"):
            return None
        results = data.get("results") or []
        return results[0] if results else None

    async def upsert_contact(
        self,
        phone: str,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        email: Optional[str] = None,
        tags: Optional[list[str]] = None,
        source: str = "voiceops-ai-agent",
    ) -> dict:
        """Upsert by phone.  Existing contact → PATCH properties.
        New contact → POST.

        Tags don't map 1:1 to HubSpot — HubSpot uses lists/lifecycle-
        stage instead.  For simplicity we stash tags into a custom
        property `voiceops_tags` (semicolon-delimited).  If the tenant
        hasn't created that property, HubSpot rejects with 400 — we
        catch and drop the tag payload instead of failing the whole
        upsert.
        """
        existing = await self.find_contact_by_phone(phone)

        # Assemble properties dict — HubSpot ignores unknown keys unless
        # they're custom properties (in which case it rejects).
        properties: dict[str, str] = {}
        if first_name:
            properties["firstname"] = first_name
        if last_name:
            properties["lastname"] = last_name
        if email:
            properties["email"] = email
        if phone:
            properties["phone"] = phone
        # Lead source — HubSpot's `hs_analytics_source` is set by them
        # only.  We use `hs_lead_status` if present; otherwise leave.
        if source:
            properties["hs_lead_status"] = "NEW"
        tag_payload = {}
        if tags:
            tag_payload = {"voiceops_tags": ";".join(tags)}

        payload = {"properties": {**properties, **tag_payload}}

        if existing and existing.get("id"):
            contact_id = existing["id"]
            try:
                data = await self._request(
                    "PATCH",
                    f"/crm/v3/objects/contacts/{contact_id}",
                    json=payload,
                )
            except HubSpotError:
                # Retry without the custom-property tag payload — the
                # tenant may not have created `voiceops_tags` yet.
                if tag_payload:
                    payload["properties"] = properties
                    data = await self._request(
                        "PATCH",
                        f"/crm/v3/objects/contacts/{contact_id}",
                        json=payload,
                    )
                else:
                    raise
            data["id"] = data.get("id") or contact_id
            return data

        # New contact
        try:
            return await self._request(
                "POST", "/crm/v3/objects/contacts", json=payload,
            )
        except HubSpotError:
            if tag_payload:
                payload["properties"] = properties
                return await self._request(
                    "POST", "/crm/v3/objects/contacts", json=payload,
                )
            raise

    # ── notes / engagements ──────────────────────────────────────────

    async def add_note(self, contact_id: str, body: str) -> dict:
        """Create a Note engagement associated with the contact.

        HubSpot v3 requires the note object with `hs_note_body` +
        `hs_timestamp` in properties, plus an association to the
        contact.  Timestamp is ms since epoch.
        """
        now_ms = int(time.time() * 1000)
        payload = {
            "properties": {
                "hs_note_body": body,
                "hs_timestamp": now_ms,
            },
            "associations": [{
                "to": {"id": contact_id},
                # Association type id 202 = note→contact per HubSpot's
                # default schema.  Documented at:
                # https://developers.hubspot.com/docs/api/crm/associations
                "types": [{
                    "associationCategory": "HUBSPOT_DEFINED",
                    "associationTypeId": 202,
                }],
            }],
        }
        return await self._request(
            "POST", "/crm/v3/objects/notes", json=payload,
        )

    # ── deals (opportunities) ────────────────────────────────────────

    async def create_deal(
        self,
        contact_id: str,
        deal_name: str,
        amount: Optional[float] = None,
        pipeline_id: Optional[str] = None,
        stage_id: Optional[str] = None,
        close_date_ms: Optional[int] = None,
    ) -> dict:
        """Create a Deal associated with the contact.

        Deals in HubSpot require `pipeline` + `dealstage`.  If the
        tenant hasn't configured these in env, we skip deal creation
        (upsert + note still happen).
        """
        if not self.create_deals:
            return {"_skipped": True, "reason": "create_deals disabled"}
        pipeline = pipeline_id or self.default_pipeline_id
        stage = stage_id or self.default_stage_id
        if not pipeline or not stage:
            return {"_skipped": True, "reason": "pipeline/stage not configured"}
        properties = {
            "dealname": deal_name,
            "pipeline": pipeline,
            "dealstage": stage,
        }
        if amount is not None:
            properties["amount"] = str(amount)
        if close_date_ms is not None:
            properties["closedate"] = str(close_date_ms)
        payload = {
            "properties": properties,
            "associations": [{
                "to": {"id": contact_id},
                # Association type id 3 = deal→contact per HubSpot
                # default schema.
                "types": [{
                    "associationCategory": "HUBSPOT_DEFINED",
                    "associationTypeId": 3,
                }],
            }],
        }
        return await self._request(
            "POST", "/crm/v3/objects/deals", json=payload,
        )

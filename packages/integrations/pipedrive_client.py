"""Pipedrive CRM v1 API client.

Auth: Pipedrive API token (per-user, retrievable from Personal Preferences
→ API in the Pipedrive web UI).  Alternative is OAuth for marketplace apps
but for a client-installed integration the API token is simpler.

Docs:
  - Persons:    https://developers.pipedrive.com/docs/api/v1/Persons
  - Deals:      https://developers.pipedrive.com/docs/api/v1/Deals
  - Notes:      https://developers.pipedrive.com/docs/api/v1/Notes
  - Activities: https://developers.pipedrive.com/docs/api/v1/Activities

Free tier (2026):
  - Developer Sandbox: no cost, indefinite (as long as owner signs in every
    45 days).  Isolated data, all API endpoints available.
  - Lite paid tier: $14/user/mo, full API.

Design notes:
  - Symmetric with `hubspot_client.HubSpotClient` — same public method
    shapes so `PipedriveSink` in `sinks.py` looks like `HubSpotSink` and
    can be swapped freely by config.
  - Same retry policy (2026-08-25 ChatGPT audit P1 — 429/5xx retry with
    Retry-After honoring + jittered exponential backoff).
  - Pipedrive's Persons object is roughly HubSpot's Contact; Deals maps
    1:1; Notes are simpler in Pipedrive (POST /notes with content +
    person_id/deal_id — no engagement scaffolding).
  - Search by phone: Pipedrive has a native `/persons/search?term=<phone>&fields=phone`
    endpoint — cleaner than HubSpot's filterGroups pattern.

Instance URL: Pipedrive is multi-region; the account owner's URL is like
`companyname.pipedrive.com/api/v1`.  Config carries `pipedrive_domain`.
"""
from __future__ import annotations

import time
from typing import Optional

import httpx


class PipedriveError(Exception):
    """Raised when a Pipedrive API call fails.  Sink callers swallow and log."""


class PipedriveClient:
    def __init__(
        self,
        api_token: str,
        company_domain: str,
        default_pipeline_id: Optional[int] = None,
        default_stage_id: Optional[int] = None,
        create_deals: bool = False,
        timeout: float = 15.0,
    ) -> None:
        if not api_token:
            raise PipedriveError("PIPEDRIVE_API_TOKEN not set")
        if not company_domain:
            raise PipedriveError(
                "PIPEDRIVE_COMPANY_DOMAIN not set (e.g. 'yourco' for "
                "yourco.pipedrive.com — do not include https:// or path)"
            )
        # Strip any accidentally-included scheme/path/dots.
        domain = company_domain.strip()
        for pref in ("https://", "http://"):
            if domain.startswith(pref):
                domain = domain[len(pref):]
        domain = domain.split("/")[0]
        if domain.endswith(".pipedrive.com"):
            domain = domain[: -len(".pipedrive.com")]
        self.company_domain = domain
        self.api_token = api_token
        self.default_pipeline_id = default_pipeline_id
        self.default_stage_id = default_stage_id
        self.create_deals = create_deals
        self.timeout = timeout

    @property
    def base_url(self) -> str:
        return f"https://{self.company_domain}.pipedrive.com/api/v1"

    # 2026-08-25 (audit P1 pattern from HubSpot client): retry semantics.
    _MAX_ATTEMPTS = 4
    _RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
    _BACKOFF_BASE_S = 0.25
    _BACKOFF_MAX_S = 8.0

    @staticmethod
    def _next_backoff(attempt: int, retry_after: Optional[str]) -> float:
        """Compute sleep time before next retry.  Honors Retry-After
        delta-seconds, caps at MAX*4, falls back to jittered exponential.
        Matches HubSpotClient._next_backoff shape exactly."""
        if retry_after:
            try:
                seconds = float(retry_after.strip())
                if 0 < seconds < PipedriveClient._BACKOFF_MAX_S * 4:
                    return seconds
            except (TypeError, ValueError):
                pass
        base = PipedriveClient._BACKOFF_BASE_S * (2 ** (attempt - 1))
        jitter = 0.7 + 0.3 * ((attempt * 17) % 10) / 10.0
        return min(base * jitter, PipedriveClient._BACKOFF_MAX_S)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> dict:
        """Pipedrive uses `?api_token=<token>` on the query string
        rather than a Bearer header.  We inject it into params so caller
        code doesn't have to think about it."""
        import asyncio
        p = dict(params or {})
        p["api_token"] = self.api_token

        last_exc: Optional[Exception] = None
        for attempt in range(1, self._MAX_ATTEMPTS + 1):
            resp = None
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.request(
                        method,
                        f"{self.base_url}{path}",
                        params=p,
                        json=json,
                    )
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                last_exc = e
                if attempt < self._MAX_ATTEMPTS:
                    await asyncio.sleep(self._next_backoff(attempt, None))
                    continue
                raise PipedriveError(
                    f"Pipedrive {method} {path} network error after "
                    f"{attempt} attempts: {e}"
                ) from e

            if resp.status_code == 404:
                # 404 → resource not found / lookup miss.  Return signal.
                return {"_not_found": True}
            if resp.status_code < 400:
                if resp.content:
                    body = resp.json()
                    # Pipedrive wraps responses as {"success": bool, "data": ...}.
                    # We return the wrapper so callers can inspect both.
                    return body
                return {}

            if resp.status_code not in self._RETRYABLE_STATUS:
                raise PipedriveError(
                    f"Pipedrive {method} {path} -> {resp.status_code}: "
                    f"{resp.text[:400]}"
                )
            if attempt < self._MAX_ATTEMPTS:
                await asyncio.sleep(self._next_backoff(
                    attempt, resp.headers.get("Retry-After"),
                ))
                continue
            raise PipedriveError(
                f"Pipedrive {method} {path} -> {resp.status_code} after "
                f"{attempt} attempts (retryable): {resp.text[:400]}"
            )
        assert last_exc is not None
        raise PipedriveError(str(last_exc))

    # ── persons (contacts) ───────────────────────────────────────────

    async def find_person_by_phone(self, phone: str) -> Optional[dict]:
        """Search by phone.  Pipedrive's persons/search endpoint takes
        `term=<phone>&fields=phone`.  Returns the first match or None."""
        if not phone:
            return None
        data = await self._request(
            "GET",
            "/persons/search",
            params={"term": phone, "fields": "phone", "limit": 1},
        )
        if data.get("_not_found"):
            return None
        items = ((data.get("data") or {}).get("items") or [])
        if items:
            # Search wraps result in {"items": [{"item": {...}, ...}]}
            first = items[0].get("item") if isinstance(items[0], dict) else None
            return first
        return None

    async def upsert_person(
        self,
        phone: str,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        email: Optional[str] = None,
        label: Optional[str] = None,
    ) -> dict:
        """Upsert by phone.  Existing → PATCH; new → POST.

        Pipedrive expects phone/email as arrays of {value, label, primary}.
        Label is Pipedrive's tag-equivalent (single string per person).
        """
        existing = await self.find_person_by_phone(phone)
        payload: dict = {}
        # Build name field — Pipedrive uses single 'name' for full name
        # OR first_name/last_name pair.  Pass both when we have them.
        if first_name or last_name:
            full = " ".join(x for x in (first_name, last_name) if x)
            payload["name"] = full
            if first_name:
                payload["first_name"] = first_name
            if last_name:
                payload["last_name"] = last_name
        if phone:
            payload["phone"] = [{
                "value": phone, "primary": True, "label": "mobile",
            }]
        if email:
            payload["email"] = [{
                "value": email, "primary": True, "label": "work",
            }]
        if label:
            payload["label"] = label

        if existing and existing.get("id"):
            person_id = existing["id"]
            data = await self._request(
                "PUT", f"/persons/{person_id}", json=payload,
            )
        else:
            data = await self._request(
                "POST", "/persons", json=payload,
            )
        # Pipedrive wraps as {"success": True, "data": {...person...}}.
        return (data.get("data") or {}) if isinstance(data, dict) else {}

    # ── notes ────────────────────────────────────────────────────────

    async def add_note(
        self,
        content: str,
        *,
        person_id: Optional[int] = None,
        deal_id: Optional[int] = None,
    ) -> dict:
        """Create a note.  MUST attach to a person OR deal (or both)."""
        payload: dict = {"content": content}
        if person_id is not None:
            payload["person_id"] = person_id
        if deal_id is not None:
            payload["deal_id"] = deal_id
        if not person_id and not deal_id:
            raise PipedriveError(
                "add_note requires person_id or deal_id"
            )
        data = await self._request("POST", "/notes", json=payload)
        return (data.get("data") or {}) if isinstance(data, dict) else {}

    # ── deals ────────────────────────────────────────────────────────

    async def create_deal(
        self,
        person_id: int,
        title: str,
        value: Optional[float] = None,
        currency: str = "EUR",
        pipeline_id: Optional[int] = None,
        stage_id: Optional[int] = None,
    ) -> dict:
        """Create a Deal associated with the person.

        Deals need pipeline_id + stage_id.  If not configured we skip
        silently (upsert + note still happen).
        """
        if not self.create_deals:
            return {"_skipped": True, "reason": "create_deals disabled"}
        pipeline = pipeline_id or self.default_pipeline_id
        stage = stage_id or self.default_stage_id
        if not pipeline or not stage:
            return {
                "_skipped": True,
                "reason": "pipeline/stage not configured",
            }
        payload: dict = {
            "title": title,
            "person_id": person_id,
            "pipeline_id": pipeline,
            "stage_id": stage,
        }
        if value is not None:
            payload["value"] = value
            payload["currency"] = currency
        data = await self._request("POST", "/deals", json=payload)
        return (data.get("data") or {}) if isinstance(data, dict) else {}

    # ── activities (bookings show up as activities in Pipedrive) ────

    async def create_activity(
        self,
        subject: str,
        due_date: str,             # YYYY-MM-DD
        due_time: str,             # HH:MM
        duration: Optional[str] = None,   # HH:MM
        activity_type: str = "meeting",   # meeting|call|task|deadline|email|lunch
        person_id: Optional[int] = None,
        deal_id: Optional[int] = None,
        note: Optional[str] = None,
    ) -> dict:
        """Create an Activity — Pipedrive's equivalent of a calendar
        event linked to a person/deal.  Useful for logging bookings
        that happen through the voice agent."""
        payload: dict = {
            "subject": subject,
            "due_date": due_date,
            "due_time": due_time,
            "type": activity_type,
        }
        if duration:
            payload["duration"] = duration
        if person_id is not None:
            payload["person_id"] = person_id
        if deal_id is not None:
            payload["deal_id"] = deal_id
        if note:
            payload["note"] = note
        data = await self._request("POST", "/activities", json=payload)
        return (data.get("data") or {}) if isinstance(data, dict) else {}

"""Pick the calendar backend based on env config.

2026-08-29 (user design pivot): factory now wraps whatever local
backend is configured with an OutboxCalendar so bookings enqueue for
later Google Calendar sync.  When Google creds appear in `.env`, a
background `CalendarOutboxSync` worker (started separately) picks
up the outbox and pushes retroactively.  Local-wins conflict
resolution per user's explicit choice.

Deploy-safe: nothing changes for callers even when creds are absent.
Local FakeCalendar remains source of truth on every read.  Outbox
JSONL is created on first write.  Sync worker is opt-in.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional


def build_calendar(backend: str, settings, business=None):
    """Construct the configured calendar backend, wrapped by the
    outbox for deferred Google sync.

    When `outbox_enabled=False` on the settings shim (default True),
    returns the bare backend — preserves the pre-2026-08-29 shape for
    any caller/test that needs the raw local calendar.  Regular
    production code path uses the wrapped version.

    Audit-3 fix (2026-08-04): accepts optional `business` so the fake
    calendar can honour BusinessProfile.hours instead of the hardcoded
    9-5 default that contradicted the Smile Dental profile.
    """
    backend = (backend or "fake").lower().strip()

    # Build the LOCAL backend (source of truth).
    if backend == "fake":
        from .fake_calendar import FakeCalendar
        hours = (
            getattr(business, "hours", None) if business is not None
            else None
        )
        local = FakeCalendar(settings.calendar_path, hours=hours)
    elif backend == "google":
        # 'google' backend historically meant "Google is my source of
        # truth."  Keep that path intact for callers who explicitly
        # opt in.  The outbox is not applied in this mode — Google IS
        # the real calendar; there's nothing to sync.
        if (
            not settings.google_service_account_json
            or not settings.google_calendar_id
        ):
            raise RuntimeError(
                "google calendar backend needs "
                "GOOGLE_SERVICE_ACCOUNT_JSON and GOOGLE_CALENDAR_ID"
            )
        from .google_calendar import GoogleCalendar
        return GoogleCalendar(
            service_account_json_path=(
                settings.google_service_account_json
            ),
            calendar_id=settings.google_calendar_id,
        )
    elif backend == "ghl":
        # 2026-09-01 GHL-wave-2 (part C): activated. Reads free-slots
        # + writes bookings directly to GHL calendar. No outbox
        # needed — GHL is the source of truth in this mode.
        if not settings.ghl_api_token or not settings.ghl_location_id:
            raise RuntimeError(
                "ghl calendar backend needs GHL_API_TOKEN and "
                "GHL_LOCATION_ID (env) or business.integrations.ghl_* "
                "fields set"
            )
        from .ghl_client import GoHighLevelClient
        from .ghl_calendar import GHLCalendar
        client = GoHighLevelClient(
            api_token=settings.ghl_api_token,
            location_id=settings.ghl_location_id,
            api_version=settings.ghl_api_version,
            default_calendar_id=settings.ghl_calendar_id,
        )
        return GHLCalendar(
            client=client,
            calendar_id=settings.ghl_calendar_id,
        )
    else:
        raise ValueError(f"unknown calendar backend: {backend}")

    # Opt-out for tests / callers that need the raw backend.
    if getattr(settings, "outbox_enabled", True) is False:
        return local

    # Wrap with OutboxCalendar so bookings enqueue for later Google
    # sync.  Remote factory returns None when creds absent — sync
    # worker will no-op.  When creds appear, sync catches up.
    from .outbox_calendar import (
        OutboxCalendar, make_google_remote_factory,
    )
    outbox_path = getattr(
        settings, "calendar_outbox_path", None,
    ) or (
        Path(settings.calendar_path).parent / "calendar_outbox.jsonl"
    )
    return OutboxCalendar(
        local=local,
        outbox_path=outbox_path,
        remote_factory=make_google_remote_factory(settings),
    )


def build_calendar_from_business(business, settings=None):
    """Construct a calendar from `business.integrations`.

    2026-09-01 GHL-wave-2 (part C): mirrors build_sink_from_business.
    Reads business.integrations.calendar_backend and the corresponding
    creds. Fallback: if the field is unset, defer to `build_calendar`
    with the global env settings so nothing changes for tenants that
    haven't opted into per-tenant config.

    Backends supported:
      * 'fake' — local JSON, honours business.hours
      * 'google' — uses integrations.google_service_account_json +
                   integrations.google_calendar_id
      * 'ghl'    — uses integrations.ghl_api_token +
                   integrations.ghl_location_id +
                   integrations.ghl_calendar_id (required for GHL cal)
    """
    integ = getattr(business, "integrations", None)
    backend = (
        getattr(integ, "calendar_backend", None) if integ else None
    ) or "fake"
    backend = backend.lower().strip()

    if backend == "fake":
        from .fake_calendar import FakeCalendar
        hours = getattr(business, "hours", None)
        # Path — reuse settings.calendar_path (per-tenant path is
        # future work; for MVP one calendar file per prod install)
        cal_path = (
            getattr(settings, "calendar_path", None) if settings
            else None
        ) or "data/calendar.json"
        return FakeCalendar(cal_path, hours=hours)

    if backend == "google":
        if not integ or not integ.google_service_account_json or not integ.google_calendar_id:
            raise RuntimeError(
                f"business {getattr(business, 'id', '?')}: "
                f"calendar_backend='google' but "
                f"integrations.google_service_account_json or "
                f"integrations.google_calendar_id is not set"
            )
        from .google_calendar import GoogleCalendar
        return GoogleCalendar(
            service_account_json_path=integ.google_service_account_json,
            calendar_id=integ.google_calendar_id,
        )

    if backend == "ghl":
        if not integ or not integ.ghl_api_token or not integ.ghl_location_id:
            raise RuntimeError(
                f"business {getattr(business, 'id', '?')}: "
                f"calendar_backend='ghl' but "
                f"integrations.ghl_api_token or "
                f"integrations.ghl_location_id is not set"
            )
        if not integ.ghl_calendar_id:
            raise RuntimeError(
                f"business {getattr(business, 'id', '?')}: "
                f"calendar_backend='ghl' also needs "
                f"integrations.ghl_calendar_id (find in GHL: Settings "
                f"→ Calendars → click your calendar → copy from URL)"
            )
        from .ghl_client import GoHighLevelClient
        from .ghl_calendar import GHLCalendar
        client = GoHighLevelClient(
            api_token=integ.ghl_api_token,
            location_id=integ.ghl_location_id,
            api_version=integ.ghl_api_version,
            default_calendar_id=integ.ghl_calendar_id,
        )
        return GHLCalendar(
            client=client,
            calendar_id=integ.ghl_calendar_id,
        )

    raise ValueError(
        f"business {getattr(business, 'id', '?')}: unknown "
        f"calendar_backend={backend!r}"
    )

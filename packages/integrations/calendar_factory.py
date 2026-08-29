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
        raise NotImplementedError(
            "GHL calendar as primary booking backend not yet wired — "
            "use ghl sink for logging and fake/google for slot selection"
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

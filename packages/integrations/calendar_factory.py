"""Pick the calendar backend based on env config."""
from __future__ import annotations

from typing import Optional


def build_calendar(backend: str, settings, business=None):
    """Construct the configured calendar backend.

    Audit-3 fix (2026-08-04): accepts optional `business` so the fake
    calendar can honour BusinessProfile.hours instead of the hardcoded
    9-5 default that contradicted the Smile Dental profile."""
    backend = (backend or "fake").lower().strip()
    if backend == "fake":
        from .fake_calendar import FakeCalendar
        hours = getattr(business, "hours", None) if business is not None else None
        return FakeCalendar(settings.calendar_path, hours=hours)
    if backend == "google":
        if not settings.google_service_account_json or not settings.google_calendar_id:
            raise RuntimeError("google calendar backend needs GOOGLE_SERVICE_ACCOUNT_JSON and GOOGLE_CALENDAR_ID")
        from .google_calendar import GoogleCalendar
        return GoogleCalendar(
            service_account_json_path=settings.google_service_account_json,
            calendar_id=settings.google_calendar_id,
        )
    if backend == "ghl":
        raise NotImplementedError("GHL calendar as primary booking backend not yet wired — use ghl sink for logging and fake/google for slot selection")
    raise ValueError(f"unknown calendar backend: {backend}")

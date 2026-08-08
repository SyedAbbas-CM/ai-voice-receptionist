"""CommitAdapter bindings for the FakeCalendar backend.

Sprint 10 WIRING (2026-08-04): implements the CommitAdapter Protocol
against packages.integrations.fake_calendar.FakeCalendar so the
CommitCoordinator can drive real bookings.

One adapter per calendar backend.  GoogleCalendar's adapter lands in
a follow-up commit — it's a straight port because FakeCalendar's
book() signature already accepts idempotency_key.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from packages.dialogue import (
    ActionKind,
    ActionProposal,
    CommitOutcome,
    CommitResult,
)

log = logging.getLogger(__name__)


class FakeCalendarBookingAdapter:
    """CommitAdapter for BOOK_APPOINTMENT against FakeCalendar."""

    def __init__(
        self,
        calendar,
        default_duration_minutes: int = 30,
        service_duration_lookup=None,
    ) -> None:
        self._calendar = calendar
        self._default_duration = default_duration_minutes
        self._service_duration_lookup = service_duration_lookup

    def _duration_for(self, service: str) -> int:
        if self._service_duration_lookup:
            try:
                return self._service_duration_lookup(service)
            except Exception:
                pass
        return self._default_duration

    async def commit(self, proposal: ActionProposal) -> CommitResult:
        """Execute the FakeCalendar.book() call.  Returns SUCCESS on
        booked, CONFLICT when the slot is no longer available, or
        PROVIDER_ERROR for anything else."""
        args = {a.name: a.value for a in proposal.arguments}
        try:
            start = args.get("start_iso")
            if isinstance(start, str):
                start = datetime.fromisoformat(start)
        except (ValueError, TypeError) as e:
            return CommitResult(
                outcome=CommitOutcome.REJECTED,
                action_id=proposal.action_id,
                error=f"bad_start_iso: {e}",
            )
        service = args.get("service") or ""
        duration = self._duration_for(service)

        try:
            outcome = self._calendar.book(
                start=start,
                duration_minutes=duration,
                caller_name=args.get("caller_name") or "",
                phone=args.get("phone") or "",
                service=service,
                notes=args.get("notes"),
                idempotency_key=proposal.idempotency_key,
            )
        except Exception as e:
            log.exception("FakeCalendar.book raised: %s", e)
            return CommitResult(
                outcome=CommitOutcome.PROVIDER_ERROR,
                action_id=proposal.action_id,
                error=f"{type(e).__name__}: {e}",
            )

        if outcome.get("booked"):
            event = outcome.get("event") or {}
            committed = {
                "caller_name": event.get("caller_name"),
                "phone": event.get("phone"),
                "service": event.get("service"),
                "start_iso": event.get("start"),
                "duration_minutes": duration,
                "notes": event.get("notes"),
            }
            return CommitResult(
                outcome=CommitOutcome.SUCCESS,
                action_id=proposal.action_id,
                external_id=event.get("id"),
                committed_values=committed,
            )
        if outcome.get("reason") == "slot no longer available":
            return CommitResult(
                outcome=CommitOutcome.CONFLICT,
                action_id=proposal.action_id,
                error="slot no longer available",
            )
        return CommitResult(
            outcome=CommitOutcome.PROVIDER_ERROR,
            action_id=proposal.action_id,
            error=str(outcome),
        )


def build_default_adapters(calendar, business=None) -> dict:
    """Convenience: build the full adapter map the coordinator needs.

    Currently just BOOK_APPOINTMENT — cancel/reschedule go through
    the ClinicToolHandler directly (they're idempotent by
    appointment_id, no dedup coordinator needed).  Sprint 11 may
    move them under the coordinator for uniform observability."""
    duration_lookup = None
    if business is not None and getattr(business, "services", None):
        service_map = {s.name.lower(): s.duration_minutes for s in business.services}
        duration_lookup = lambda name: service_map.get(name.lower(), 30)
    return {
        ActionKind.BOOK_APPOINTMENT: FakeCalendarBookingAdapter(
            calendar=calendar,
            service_duration_lookup=duration_lookup,
        ),
    }

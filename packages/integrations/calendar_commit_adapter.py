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
        business=None,
    ) -> None:
        self._calendar = calendar
        self._default_duration = default_duration_minutes
        self._service_duration_lookup = service_duration_lookup
        # 2026-08-08: business profile passed in so confirmation
        # SMS/email can reference the real name/address/phone.
        self._business = business

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
            # 2026-08-08 (task #275/#276): fire SMS + email confirmations.
            # Fire-and-forget — never block the booking on messaging.
            # Both handlers gracefully no-op if not configured (missing
            # SENDGRID_API_KEY, missing phone number, etc).
            self._fire_confirmations_bg(
                event=event, duration=duration, business=self._business,
                caller_email=args.get("email"),
                confirmation_id=event.get("id"),
            )
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

    def _fire_confirmations_bg(
        self, event: dict, duration: int, business,
        caller_email: Optional[str], confirmation_id: Optional[str],
    ) -> None:
        """Fire SMS + email confirmations without blocking the booking.

        Everything is best-effort.  Missing credentials, missing phone,
        network errors, template failures — all logged + swallowed.
        The BOOKING is already successful when this runs; messaging is
        courtesy, not part of the commit transaction."""
        import asyncio
        phone = event.get("phone") or ""
        service = event.get("service") or "appointment"
        start_str = event.get("start") or ""
        biz_name = getattr(business, "name", None) if business is not None else "the clinic"
        biz_address = getattr(business, "address", None) if business is not None else None
        biz_phone = getattr(business, "phone", None) if business is not None else None

        # Human-friendly "Sat Nov 22 at 2:00 PM" from ISO
        when_human = start_str
        try:
            _start_dt = datetime.fromisoformat(start_str) if start_str else None
            if _start_dt is not None:
                when_human = _start_dt.strftime("%a %b %d at %-I:%M %p")
        except Exception:
            _start_dt = None

        # SMS
        if phone and phone.startswith("+"):
            try:
                from packages.integrations.sms_sender import (
                    render_confirmation, send_sms,
                )
                body = render_confirmation(
                    business_name=biz_name, service=service,
                    when_human=when_human, confirmation_id=confirmation_id,
                )
                asyncio.create_task(
                    send_sms(phone, body),
                    name=f"sms-conf-{confirmation_id or 'x'}",
                )
            except Exception as e:
                log.warning("SMS confirmation dispatch failed: %s", e)

        # Email
        if caller_email and "@" in caller_email:
            try:
                from packages.integrations.email_sender import (
                    _build_ics, render_confirmation_html, send_confirmation_email,
                )
                subject, html = render_confirmation_html(
                    business_name=biz_name, service=service,
                    when_human=when_human, address=biz_address,
                    phone=biz_phone, confirmation_id=confirmation_id,
                )
                ics = None
                if _start_dt is not None:
                    ics = _build_ics(
                        biz_name, service, _start_dt, duration,
                        location=biz_address,
                    )
                asyncio.create_task(
                    send_confirmation_email(caller_email, subject, html, ics),
                    name=f"email-conf-{confirmation_id or 'x'}",
                )
            except Exception as e:
                log.warning("Email confirmation dispatch failed: %s", e)


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
            business=business,
        ),
    }

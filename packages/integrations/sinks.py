"""CRM sinks — write-side integrations that log call outcomes.

The brain and session manager fire two events:
  - on_booking(state, booking_payload)  after a successful book_appointment tool call
  - on_call_end(state)                  when the call ends (voluntary or hangup)

Each sink swallows its own errors so a broken CRM never crashes the call flow.
"""
from __future__ import annotations

import html as _html
import logging
import re as _re
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

from packages.schemas import CallState


# 2026-08-25 (security-review finding): caller-controlled fields
# (caller_name, caller_phone, service, etc.) come from tool arguments
# populated by the LLM from CALLER SPEECH.  They must never be
# interpolated raw into HTML (email body → stored XSS in Gmail/Outlook)
# or into an email Subject line (\r\n → header injection: attacker
# adds a Bcc: header that copies the booking notification elsewhere).
#
# Every string that flows from a tool argument into HTML MUST pass
# through _safe_html().  Every string that flows into an email header
# MUST pass through _safe_header().
def _safe_html(value: object) -> str:
    """HTML-escape any value for safe inclusion in an HTML email body.
    None → empty string."""
    if value is None:
        return ""
    return _html.escape(str(value), quote=True)


# Match all ASCII control characters — CR, LF, NUL, and other C0 control
# codes.  An email header containing any of these can smuggle new
# headers or terminate the header block early.
_HEADER_CONTROL_CHARS = _re.compile(r"[\x00-\x1F\x7F]")


def _safe_header(value: object, *, max_len: int = 200) -> str:
    """Strip control characters (CR/LF/NUL) from a value destined for
    an email Subject or other header.  Also truncates to `max_len` so
    an unbounded caller string can't bloat the header past MTA limits.
    """
    if value is None:
        return ""
    cleaned = _HEADER_CONTROL_CHARS.sub("", str(value))
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len]
    return cleaned


log = logging.getLogger(__name__)


class CRMSink(ABC):
    name: str = "base"

    @abstractmethod
    async def on_booking(self, state: CallState, booking: dict) -> None:
        ...

    @abstractmethod
    async def on_call_end(self, state: CallState) -> None:
        ...


class NoopSink(CRMSink):
    name = "none"

    async def on_booking(self, state: CallState, booking: dict) -> None:
        return None

    async def on_call_end(self, state: CallState) -> None:
        return None


class CompositeSink(CRMSink):
    """Fan out to multiple sinks. Each is best-effort."""

    name = "composite"

    def __init__(self, sinks: list[CRMSink]) -> None:
        self.sinks = sinks

    async def on_booking(self, state: CallState, booking: dict) -> None:
        for s in self.sinks:
            try:
                await s.on_booking(state, booking)
            except Exception as e:
                log.warning("sink %s on_booking failed: %s", s.name, e)

    async def on_call_end(self, state: CallState) -> None:
        for s in self.sinks:
            try:
                await s.on_call_end(state)
            except Exception as e:
                log.warning("sink %s on_call_end failed: %s", s.name, e)


class GHLSink(CRMSink):
    """Upsert contact + add note + book appointment on GHL calendar (if configured)."""

    name = "ghl"

    def __init__(self, client, business=None) -> None:
        self.client = client  # GoHighLevelClient
        # 2026-08-31 GHL-SMS wave 1: business profile is needed for
        # send_sms_on_booking + sms_confirmation_template. Optional
        # (backwards compatible — SMS is off unless business is
        # provided AND biz.send_sms_on_booking=True OR the env fallback
        # GHL_SMS_ON_BOOKING=true is set).
        self.business = business

    def _split_name(self, full: Optional[str]) -> tuple[Optional[str], Optional[str]]:
        if not full:
            return None, None
        parts = full.strip().split(maxsplit=1)
        return parts[0], parts[1] if len(parts) > 1 else None

    async def on_booking(self, state: CallState, booking: dict) -> None:
        args = booking.get("arguments") or {}
        result = booking.get("result") or {}
        if not result.get("booked"):
            return
        phone = args.get("phone") or state.extracted.phone
        if not phone:
            return
        first, last = self._split_name(args.get("caller_name") or state.extracted.caller_name)
        contact = await self.client.upsert_contact(
            phone=phone,
            first_name=first,
            last_name=last,
            tags=["voiceops-ai-agent", state.extracted.intent.value if state.extracted else "unknown"],
        )
        contact_id = contact.get("id") or (contact.get("contact") or {}).get("id")
        if not contact_id:
            return
        summary = state.extracted.summary if state.extracted else ""
        await self.client.add_note(contact_id, f"Booked via AI receptionist.\n{summary}\nBooking: {args}")

        event = result.get("event") or {}
        start = event.get("start")
        if start and self.client.default_calendar_id:
            try:
                await self.client.book_appointment(
                    contact_id=contact_id,
                    start=datetime.fromisoformat(start),
                    duration_minutes=30,
                    title=f"{args.get('service', 'Appointment')} — {args.get('caller_name', '')}",
                    notes=args.get("notes"),
                )
            except Exception as e:
                log.warning("ghl book_appointment failed: %s", e)

        # 2026-08-31 GHL-SMS wave 1: fire confirmation SMS to the caller.
        # Feature-flagged so nobody accidentally texts callers before
        # setup. Business config exposes:
        #   send_sms_on_booking: bool = True to enable
        #   sms_confirmation_template: str = custom template
        # Falls back to a sensible default template. Never raises —
        # a failed SMS must not tank the booking flow.
        try:
            import os as _os
            send_sms = _os.environ.get("GHL_SMS_ON_BOOKING", "false").lower() in ("1", "true", "yes")
            biz = self.business or getattr(state, "business", None)
            if biz is not None:
                send_sms = getattr(biz, "send_sms_on_booking", send_sms)
            if send_sms and phone:
                # Format the SMS body — kept under 160 chars for single-part SMS
                biz_name = getattr(biz, "name", "our clinic") if biz else "our clinic"
                svc = args.get("service", "your appointment")
                # Parse the start time into a readable "Tuesday at 2 PM"
                sms_when = start
                if start:
                    try:
                        _dt = datetime.fromisoformat(start)
                        sms_when = _dt.strftime("%A at %-I:%M %p")
                    except Exception:
                        sms_when = start
                template = None
                if biz is not None:
                    template = getattr(biz, "sms_confirmation_template", None)
                if not template:
                    template = (
                        "Hi {first_name}, this is {business_name} confirming "
                        "your {service} on {when}. See you then!"
                    )
                sms_body = template.format(
                    first_name=first or "there",
                    business_name=biz_name,
                    service=svc,
                    when=sms_when,
                )[:320]  # cap at 2 SMS segments
                await self.client.send_sms(contact_id, sms_body)
                log.info(
                    "GHL_SMS_SENT contact=%s phone=%s body=%r",
                    contact_id, phone, sms_body[:80],
                )
        except Exception as e:
            log.warning("ghl send_sms on_booking failed: %s", e)

    async def on_call_end(self, state: CallState) -> None:
        if not state.extracted or not state.extracted.phone:
            return
        first, last = self._split_name(state.extracted.caller_name)
        try:
            contact = await self.client.upsert_contact(
                phone=state.extracted.phone,
                first_name=first,
                last_name=last,
                tags=["voiceops-ai-agent"],
            )
            contact_id = contact.get("id") or (contact.get("contact") or {}).get("id")
            if contact_id:
                lines = [
                    f"Session: {state.session_id}",
                    f"Intent: {state.extracted.intent.value}",
                    f"Urgency: {state.extracted.urgency.value}",
                    f"Lead score: {state.extracted.lead_score}",
                    f"Summary: {state.extracted.summary}",
                    f"Status: {state.status.value if hasattr(state.status, 'value') else state.status}",
                ]
                await self.client.add_note(contact_id, "\n".join(lines))
        except Exception as e:
            log.warning("ghl on_call_end failed: %s", e)


class SheetsSink(CRMSink):
    """Append one row per completed call to a Google Sheet."""

    name = "sheets"

    def __init__(self, sheets) -> None:
        self.sheets = sheets  # GoogleSheets

    async def on_booking(self, state: CallState, booking: dict) -> None:
        return None  # we log at call-end so each call is one row

    async def on_call_end(self, state: CallState) -> None:
        extracted = state.extracted.model_dump() if state.extracted else {}
        status = state.status.value if hasattr(state.status, "value") else state.status
        escalated = status == "escalated"
        try:
            self.sheets.append_call(
                session_id=state.session_id,
                extracted=extracted,
                status=status,
                escalated=escalated,
            )
        except Exception as e:
            log.warning("sheets append_call failed: %s", e)


class HubSpotSink(CRMSink):
    """Upsert contact + add note + optionally create a deal on HubSpot.

    Free-tier friendly: contact upsert + note engagement work on
    HubSpot Free.  Deal creation requires a pipeline + stage — off by
    default; enable via settings.hubspot_create_deals + configure
    hubspot_pipeline_id + hubspot_stage_id.

    Failure policy: mirrors GHLSink — swallow at sink boundary, log
    warnings.  A broken CRM must never crash the call.
    """

    name = "hubspot"

    def __init__(self, client) -> None:
        self.client = client  # HubSpotClient

    @staticmethod
    def _split_name(full: Optional[str]) -> tuple[Optional[str], Optional[str]]:
        if not full:
            return None, None
        parts = full.strip().split(maxsplit=1)
        return parts[0], parts[1] if len(parts) > 1 else None

    async def on_booking(self, state: CallState, booking: dict) -> None:
        args = booking.get("arguments") or {}
        result = booking.get("result") or {}
        # Booking tool signals success either as {"booked": True} (GHL
        # calendar path) or {"ok": True} (local calendar).  Accept
        # either — the sink is transport-agnostic.
        if not (result.get("booked") or result.get("ok")):
            return
        # Prefer the tool's own arguments (canonical, already validated)
        # over the extractor's fields.  Fall back to extracted when the
        # LLM booked using data from earlier in the conversation.
        extracted = state.extracted
        phone = (
            args.get("phone")
            or (extracted.phone if extracted else None)
        )
        if not phone:
            return
        caller_name = (
            args.get("caller_name")
            or (extracted.caller_name if extracted else None)
        )
        first, last = self._split_name(caller_name)
        email = args.get("email") or None

        # Tags: single global tag + intent so downstream reports can
        # bucket by "AI receptionist" and by "booking vs enquiry".
        tags = ["voiceops-ai-agent"]
        if extracted and extracted.intent:
            tags.append(extracted.intent.value)

        try:
            contact = await self.client.upsert_contact(
                phone=phone,
                first_name=first,
                last_name=last,
                email=email,
                tags=tags,
            )
        except Exception as e:
            log.warning("hubspot upsert_contact failed: %s", e)
            return

        contact_id = contact.get("id")
        if not contact_id:
            return

        # Note engagement — the human-readable record of what got booked.
        summary = extracted.summary if extracted else ""
        note_lines = [
            "Booked via AI receptionist.",
            "",
            f"Service: {args.get('service', '?')}",
            f"When: {args.get('start_iso') or args.get('date') + ' ' + args.get('time', '') if args.get('date') else '?'}",
            f"Phone: {phone}",
        ]
        if summary:
            note_lines.append("")
            note_lines.append(f"Summary: {summary}")
        try:
            await self.client.add_note(contact_id, "\n".join(note_lines))
        except Exception as e:
            log.warning("hubspot add_note failed: %s", e)

        # Deal creation is optional and gated by client config.
        try:
            deal_name = (
                f"{args.get('service', 'Appointment')} — {caller_name or phone}"
            )
            await self.client.create_deal(
                contact_id=contact_id,
                deal_name=deal_name,
            )
        except Exception as e:
            log.warning("hubspot create_deal failed: %s", e)

    async def on_call_end(self, state: CallState) -> None:
        """Log a note on every call end so tenants see missed calls +
        enquiries too, not only successful bookings."""
        if not state.extracted or not state.extracted.phone:
            return
        first, last = self._split_name(state.extracted.caller_name)
        try:
            contact = await self.client.upsert_contact(
                phone=state.extracted.phone,
                first_name=first,
                last_name=last,
                tags=["voiceops-ai-agent"],
            )
        except Exception as e:
            log.warning("hubspot upsert_contact (call_end) failed: %s", e)
            return

        contact_id = contact.get("id")
        if not contact_id:
            return

        status = (
            state.status.value
            if hasattr(state.status, "value") else str(state.status)
        )
        note_lines = [
            f"Call ended — status: {status}",
            f"Session: {state.session_id}",
            f"Intent: {state.extracted.intent.value}",
            f"Urgency: {state.extracted.urgency.value}",
            f"Lead score: {state.extracted.lead_score}",
        ]
        if state.extracted.summary:
            note_lines.append("")
            note_lines.append(f"Summary: {state.extracted.summary}")
        try:
            await self.client.add_note(contact_id, "\n".join(note_lines))
        except Exception as e:
            log.warning("hubspot add_note (call_end) failed: %s", e)


class PipedriveSink(CRMSink):
    """Upsert person + add note + optionally create deal + create activity
    on Pipedrive.

    2026-08-25 (EU demo pass): job brief explicitly names Pipedrive as a
    supported CRM.  Free Developer Sandbox is real-account-quality — no
    special sandbox behavior, just isolated data.

    Failure policy mirrors GHLSink / HubSpotSink — swallow at sink
    boundary, log warnings.  A broken CRM must never crash the call.
    """

    name = "pipedrive"

    def __init__(self, client) -> None:
        self.client = client  # PipedriveClient

    @staticmethod
    def _split_name(full: Optional[str]) -> tuple[Optional[str], Optional[str]]:
        if not full:
            return None, None
        parts = full.strip().split(maxsplit=1)
        return parts[0], parts[1] if len(parts) > 1 else None

    async def on_booking(self, state: CallState, booking: dict) -> None:
        args = booking.get("arguments") or {}
        result = booking.get("result") or {}
        # Accept both {"booked": True} (GHL/calendar path) and
        # {"ok": True} (local path).
        if not (result.get("booked") or result.get("ok")):
            return
        extracted = state.extracted
        phone = (
            args.get("phone")
            or (extracted.phone if extracted else None)
        )
        if not phone:
            return
        caller_name = (
            args.get("caller_name")
            or (extracted.caller_name if extracted else None)
        )
        first, last = self._split_name(caller_name)
        email = args.get("email") or None

        # Pipedrive's `label` field is a single string, not a list.  Pick
        # the most useful marker for downstream filtering.
        label = "voiceops-ai-agent"

        try:
            person = await self.client.upsert_person(
                phone=phone,
                first_name=first,
                last_name=last,
                email=email,
                label=label,
            )
        except Exception as e:
            log.warning("pipedrive upsert_person failed: %s", e)
            return

        person_id = person.get("id")
        if not person_id:
            return

        # Note — human-readable summary of the booking.
        service = args.get("service") or "Appointment"
        when = (
            args.get("start_iso")
            or (args.get("date", "") + " " + args.get("time", "")).strip()
        )
        summary = extracted.summary if extracted else ""
        note_lines = [
            f"<p><b>Booked via AI receptionist.</b></p>",
            f"<p>Service: {service}</p>",
            f"<p>When: {when}</p>",
            f"<p>Phone: {phone}</p>",
        ]
        if summary:
            note_lines.append(f"<p>Summary: {summary}</p>")
        try:
            await self.client.add_note(
                content="\n".join(note_lines),
                person_id=int(person_id),
            )
        except Exception as e:
            log.warning("pipedrive add_note failed: %s", e)

        # Activity — represents the booking as a Pipedrive activity.
        try:
            if args.get("start_iso"):
                # Extract date + time from start_iso "YYYY-MM-DDTHH:MM".
                iso = str(args["start_iso"])
                date_part, _, time_part = iso.partition("T")
                due_time = (time_part.split(":")[0]
                             + ":"
                             + time_part.split(":")[1]) if ":" in time_part else "09:00"
                duration = args.get("duration_minutes") or 30
                dur_str = f"{int(duration) // 60:02d}:{int(duration) % 60:02d}"
                await self.client.create_activity(
                    subject=f"{service} — {caller_name or phone}",
                    due_date=date_part,
                    due_time=due_time,
                    duration=dur_str,
                    activity_type="meeting",
                    person_id=int(person_id),
                    note=args.get("notes") or "",
                )
        except Exception as e:
            log.warning("pipedrive create_activity failed: %s", e)

        # Deal — optional, gated by client config.
        try:
            deal_title = (
                f"{service} — {caller_name or phone}"
            )
            await self.client.create_deal(
                person_id=int(person_id),
                title=deal_title,
            )
        except Exception as e:
            log.warning("pipedrive create_deal failed: %s", e)

    async def on_call_end(self, state: CallState) -> None:
        """Log a summary note on call end so missed calls + enquiries
        also land in Pipedrive, not only successful bookings."""
        if not state.extracted or not state.extracted.phone:
            return
        first, last = self._split_name(state.extracted.caller_name)
        try:
            person = await self.client.upsert_person(
                phone=state.extracted.phone,
                first_name=first,
                last_name=last,
                label="voiceops-ai-agent",
            )
        except Exception as e:
            log.warning("pipedrive upsert_person (call_end) failed: %s", e)
            return
        person_id = person.get("id")
        if not person_id:
            return
        status = (
            state.status.value
            if hasattr(state.status, "value") else str(state.status)
        )
        lines = [
            f"<p><b>Call ended — status: {status}</b></p>",
            f"<p>Session: {state.session_id}</p>",
            f"<p>Intent: {state.extracted.intent.value}</p>",
            f"<p>Urgency: {state.extracted.urgency.value}</p>",
            f"<p>Lead score: {state.extracted.lead_score}</p>",
        ]
        if state.extracted.summary:
            lines.append(f"<p>Summary: {state.extracted.summary}</p>")
        try:
            await self.client.add_note(
                content="\n".join(lines), person_id=int(person_id),
            )
        except Exception as e:
            log.warning("pipedrive add_note (call_end) failed: %s", e)


class WebhookSink(CRMSink):
    """Emit business events to a tenant-configured URL.

    2026-08-26 — real-estate + car-wash job briefs both named
    n8n/Make/Zapier as required.  This sink is the "we integrate with
    any workflow platform" answer: tenant runs their own n8n/Make/
    Zapier instance and points a Webhook trigger at the URL we POST to.

    Same failure-isolation as every other CRMSink — if the tenant's
    workflow URL is down, HubSpot writes still fire, SMS still sends,
    calendar still books.  The whole point is that CRMs, calendar,
    SMS, and workflows are all INDEPENDENT downstream consumers of
    the same call.

    Events emitted (aligns with `docs/WEBHOOK-EVENT-SCHEMA.md`):
      - booking.created (on successful on_booking with book_* tool
        result marked ok/booked)
      - call.completed (on on_call_end)
      - missed_call (future — needs the /twilio/status missed-call
        branch we haven't wired yet; scaffolded)
      - message.taken (future — needs TakeMessage tool, task #121)
      - transfer.requested (on escalate_to_human tool receipt)
    """

    name = "webhook"

    def __init__(self, client) -> None:
        self.client = client  # WebhookClient

    async def on_booking(self, state: CallState, booking: dict) -> None:
        args = booking.get("arguments") or {}
        result = booking.get("result") or {}
        if not (result.get("booked") or result.get("ok")):
            return
        # Event payload matches the canonical schema.  Field names are
        # snake_case, no nested dicts deeper than 2 levels for n8n
        # ergonomics (nested paths are tedious in n8n's Expression editor).
        payload = {
            "tenant_id": getattr(state, "tenant_id", None),
            "business_id": getattr(state, "business_id", None),
            "session_id": state.session_id,
            "call_sid": state.session_id.removeprefix("twilio_")
                if state.session_id.startswith("twilio_") else None,
            "caller_name": (
                args.get("caller_name")
                or (state.extracted.caller_name if state.extracted else None)
            ),
            "phone": (
                args.get("phone")
                or (state.extracted.phone if state.extracted else None)
            ),
            "email": args.get("email"),
            "service": args.get("service"),
            "start_iso": args.get("start_iso"),
            "duration_minutes": args.get("duration_minutes"),
            "notes": args.get("notes"),
            "tool_name": booking.get("name"),
            "booked_at": None,   # timestamp filled in envelope
        }
        try:
            await self.client.emit(
                event_type="booking.created",
                payload=payload,
                idempotency_key=(
                    f"booking:{state.session_id}:{booking.get('name')}"
                ),
            )
        except Exception as e:
            log.warning("webhook booking.created failed: %s", e)

    async def on_call_end(self, state: CallState) -> None:
        extracted = state.extracted
        status = (
            state.status.value
            if hasattr(state.status, "value") else str(state.status)
        )
        payload = {
            "tenant_id": getattr(state, "tenant_id", None),
            "business_id": getattr(state, "business_id", None),
            "session_id": state.session_id,
            "call_sid": state.session_id.removeprefix("twilio_")
                if state.session_id.startswith("twilio_") else None,
            "status": status,
            "caller_name": extracted.caller_name if extracted else None,
            "phone": extracted.phone if extracted else None,
            "intent": extracted.intent.value if extracted else None,
            "urgency": extracted.urgency.value if extracted else None,
            "lead_score": extracted.lead_score if extracted else None,
            "summary": extracted.summary if extracted else None,
            "escalation_reason": getattr(state, "escalation_reason", None),
        }
        try:
            await self.client.emit(
                event_type="call.completed",
                payload=payload,
                idempotency_key=f"call_end:{state.session_id}",
            )
        except Exception as e:
            log.warning("webhook call.completed failed: %s", e)


class FollowupSink(CRMSink):
    """Fire caller SMS + owner email on successful bookings.

    Design (2026-08-24):
      - Caller gets an SMS confirmation immediately after booking success.
        Real receptionists confirm verbally + follow up with text; this
        matches that expectation.
      - Owner gets an email with the booking details.  Owner's email
        comes from `business.email` or env `OWNER_EMAIL_OVERRIDE`.
      - Both are best-effort: SMS credit exhausted / SMTP down never
        crashes the call.

    Compliance:
      - SMS body includes "Reply STOP to unsubscribe" via
        `render_confirmation` (TCPA + Twilio AUP).
      - Email includes ICS attachment so caller can add appointment
        to their calendar.

    Not fired on `on_call_end` — that's for owner-side call summaries.
    We'd want a separate "call summary email" hook there once the
    schema for CallEndEvent lands.  For now `on_call_end` is a no-op.
    """

    name = "followup"

    def __init__(
        self,
        business_name: str,
        owner_email: Optional[str],
        location: Optional[str] = None,
        send_sms_to_caller: bool = True,
        send_email_to_owner: bool = True,
    ) -> None:
        self.business_name = business_name
        self.owner_email = owner_email
        self.location = location
        self.send_sms_to_caller = send_sms_to_caller
        self.send_email_to_owner = send_email_to_owner

    @staticmethod
    def _when_human(args: dict) -> Optional[str]:
        """Render 'Tuesday, August 26 at 2:30 PM' from booking args."""
        start_iso = args.get("start_iso") or args.get("start")
        if start_iso and isinstance(start_iso, str):
            try:
                dt = datetime.fromisoformat(start_iso.rstrip("Z"))
                return dt.strftime("%A, %B %d at %I:%M %p").lstrip("0")
            except (TypeError, ValueError):
                pass
        # Fall back to date + time as-supplied.
        date = args.get("date") or ""
        time = args.get("time") or ""
        if date and time:
            return f"{date} at {time}"
        return date or time or None

    @staticmethod
    def _parse_start(args: dict) -> Optional[datetime]:
        start_iso = args.get("start_iso") or args.get("start")
        if start_iso and isinstance(start_iso, str):
            try:
                return datetime.fromisoformat(start_iso.rstrip("Z"))
            except (TypeError, ValueError):
                pass
        return None

    async def on_booking(self, state: CallState, booking: dict) -> None:
        args = booking.get("arguments") or {}
        result = booking.get("result") or {}
        if not (result.get("booked") or result.get("ok")):
            return

        service = args.get("service") or "your appointment"
        when_human = self._when_human(args)
        caller_phone = args.get("phone") or (
            state.extracted.phone if state.extracted else None
        )
        caller_email = args.get("email") or None

        # ── Caller SMS ────────────────────────────────────────────
        if self.send_sms_to_caller and caller_phone and when_human:
            try:
                from .sms_sender import render_confirmation, send_sms
                body = render_confirmation(
                    business_name=self.business_name,
                    service=service,
                    when_human=when_human,
                )
                res = await send_sms(to=caller_phone, body=body)
                if not res.ok:
                    log.warning(
                        "followup sms to %s failed: %s",
                        caller_phone, res.error,
                    )
            except Exception as e:
                log.warning("followup sms exception: %s", e)

        # ── Owner email ───────────────────────────────────────────
        if self.send_email_to_owner and self.owner_email and when_human:
            try:
                from .email_sender import (
                    _build_ics,
                    render_confirmation_html,
                    send_confirmation_email,
                )
                start = self._parse_start(args)
                duration = int(args.get("duration_minutes") or 30)
                caller_name = (
                    args.get("caller_name")
                    or (state.extracted.caller_name if state.extracted else None)
                    or "New caller"
                )
                # Owner subject makes it obvious in an inbox WHO booked
                # WHAT, WHEN — not the standard caller-facing subject.
                # `_safe_header` strips control chars (CR/LF/NUL) that
                # would let a malicious caller name inject additional
                # email headers (e.g. Bcc: attacker@evil.com) via the
                # subject line.  See security-review finding 2026-08-25.
                subject_override = _safe_header(
                    f"New booking — {caller_name} for {service} on {when_human}"
                )
                _, html_body = render_confirmation_html(
                    business_name=self.business_name,
                    service=service,
                    when_human=when_human,
                    phone=caller_phone,
                    address=self.location,
                )
                # Prepend owner-facing summary paragraph.  Every
                # caller-controlled field is HTML-escaped before
                # interpolation — otherwise a caller saying
                # "my name is <script>fetch('/steal')</script>" would
                # yield a stored XSS in the owner's inbox.
                _safe_caller_name = _safe_html(caller_name)
                _safe_caller_phone = _safe_html(caller_phone)
                owner_intro = (
                    f"<p><b>New booking through the AI receptionist.</b></p>"
                    f"<p>Caller: {_safe_caller_name}"
                    + (f" ({_safe_caller_phone})" if caller_phone else "")
                    + "</p>"
                )
                html_body = owner_intro + html_body

                ics_content = None
                if start is not None:
                    ics_content = _build_ics(
                        business_name=self.business_name,
                        service=service,
                        start=start,
                        duration_minutes=duration,
                        location=self.location,
                    )
                res = await send_confirmation_email(
                    to=self.owner_email,
                    subject=subject_override,
                    html_body=html_body,
                    ics_content=ics_content,
                )
                if not res.ok:
                    log.warning(
                        "followup email to %s failed: %s",
                        self.owner_email, res.error,
                    )
            except Exception as e:
                log.warning("followup email exception: %s", e)

    async def on_call_end(self, state: CallState) -> None:
        # 2026-08-24: not wired yet.  Belongs to the "call summary
        # email to owner" flow once the Twilio /twilio/status endpoint
        # is emitting CallEndEvent to the outbox.  Placeholder no-op.
        return None


def build_sink_from_env(mode: str, settings, business=None) -> CRMSink:
    """Factory: 'none' | 'ghl' | 'sheets' | 'hubspot' | combos with '+'.

    Examples:
      'hubspot'          → HubSpotSink alone
      'ghl+sheets'       → GHL primary + Sheets audit log
      'hubspot+sheets'   → HubSpot primary + Sheets audit log
      'none' (default)   → NoopSink; nothing writes anywhere

    `business` — optional BusinessProfile so per-tenant sink features
    (SMS confirmation template, etc.) can read from the profile.
    """
    mode = (mode or "none").lower().strip()
    if mode == "none":
        return NoopSink()

    sinks: list[CRMSink] = []
    parts = {p.strip() for p in mode.split("+")}

    if "ghl" in parts:
        from .ghl_client import GoHighLevelClient
        client = GoHighLevelClient(
            api_token=settings.ghl_api_token or "",
            location_id=settings.ghl_location_id or "",
            api_version=settings.ghl_api_version,
            default_calendar_id=settings.ghl_calendar_id,
        )
        sinks.append(GHLSink(client, business=business))

    if "hubspot" in parts:
        from .hubspot_client import HubSpotClient
        if not getattr(settings, "hubspot_access_token", None):
            raise RuntimeError(
                "hubspot sink requires HUBSPOT_ACCESS_TOKEN — generate a "
                "Private App token at Settings → Integrations → Private "
                "Apps in HubSpot and set it in env"
            )
        client = HubSpotClient(
            access_token=settings.hubspot_access_token,
            portal_id=getattr(settings, "hubspot_portal_id", None),
            default_pipeline_id=getattr(settings, "hubspot_pipeline_id", None),
            default_stage_id=getattr(settings, "hubspot_stage_id", None),
            create_deals=bool(getattr(settings, "hubspot_create_deals", False)),
        )
        sinks.append(HubSpotSink(client))

    if "webhook" in parts:
        from .webhook_client import WebhookClient
        webhook_url = getattr(settings, "webhook_url", None)
        webhook_secret = getattr(settings, "webhook_secret", None)
        if not webhook_url:
            raise RuntimeError(
                "webhook sink requires WEBHOOK_URL — the tenant's n8n / "
                "Make / Zapier trigger URL"
            )
        if not webhook_secret:
            raise RuntimeError(
                "webhook sink requires WEBHOOK_SECRET — generate with "
                "`openssl rand -hex 32` and share with the tenant so "
                "their workflow can verify our HMAC signature"
            )
        client = WebhookClient(
            url=webhook_url,
            secret=webhook_secret,
            source=getattr(settings, "webhook_source", "voiceops-ai-agent"),
        )
        sinks.append(WebhookSink(client))

    if "pipedrive" in parts:
        from .pipedrive_client import PipedriveClient
        if not getattr(settings, "pipedrive_api_token", None):
            raise RuntimeError(
                "pipedrive sink requires PIPEDRIVE_API_TOKEN — see your "
                "user Personal Preferences → API in the Pipedrive web UI"
            )
        if not getattr(settings, "pipedrive_company_domain", None):
            raise RuntimeError(
                "pipedrive sink requires PIPEDRIVE_COMPANY_DOMAIN — the "
                "subdomain before .pipedrive.com in your account URL"
            )
        client = PipedriveClient(
            api_token=settings.pipedrive_api_token,
            company_domain=settings.pipedrive_company_domain,
            default_pipeline_id=getattr(
                settings, "pipedrive_pipeline_id", None,
            ),
            default_stage_id=getattr(settings, "pipedrive_stage_id", None),
            create_deals=bool(getattr(
                settings, "pipedrive_create_deals", False,
            )),
        )
        sinks.append(PipedriveSink(client))

    if "sheets" in parts:
        from .google_sheets import GoogleSheets
        if not settings.google_service_account_json or not settings.google_sheet_id:
            raise RuntimeError("sheets sink requires GOOGLE_SERVICE_ACCOUNT_JSON and GOOGLE_SHEET_ID")
        sheets = GoogleSheets(
            service_account_json_path=settings.google_service_account_json,
            sheet_id=settings.google_sheet_id,
            tab=settings.google_sheet_tab,
        )
        sinks.append(SheetsSink(sheets))

    if "followup" in parts:
        # FollowupSink fires SMS to caller + email to owner on booking
        # success.  Needs the business name + owner email at construction
        # time.  Business_name/location come from the loaded profile —
        # config falls back to env override for demos.
        business_name = (
            getattr(settings, "followup_business_name", None)
            or getattr(settings, "business_name_override", None)
            or "Reception"
        )
        owner_email = (
            getattr(settings, "followup_owner_email", None)
            or getattr(settings, "owner_email_override", None)
        )
        location = getattr(settings, "followup_business_address", None)
        sinks.append(FollowupSink(
            business_name=business_name,
            owner_email=owner_email,
            location=location,
            send_sms_to_caller=bool(getattr(
                settings, "followup_sms_caller", True,
            )),
            send_email_to_owner=bool(getattr(
                settings, "followup_email_owner", True,
            )),
        ))

    if not sinks:
        return NoopSink()
    if len(sinks) == 1:
        return sinks[0]
    return CompositeSink(sinks)


def build_sink_from_business(business) -> CRMSink:
    """Construct a CRMSink from the tenant's business.integrations.

    2026-09-01 GHL-wave-2: per-tenant sink construction. Reads
    business.integrations.crm_sinks (a list like ['ghl', 'sheets'])
    and the corresponding token fields. Every backend is independent —
    unused ones' fields can be blank.

    Empty crm_sinks list → NoopSink (nothing writes anywhere). This
    is the correct default for a tenant that hasn't onboarded any
    CRM yet.

    Returns:
      - NoopSink if crm_sinks is empty
      - The single sink if only one is configured
      - CompositeSink if multiple

    Raises RuntimeError with a specific per-sink message if the sink
    is listed but its required creds are missing (e.g. 'ghl' in list
    but ghl_api_token is None).
    """
    integ = getattr(business, "integrations", None)
    if integ is None or not getattr(integ, "crm_sinks", None):
        return NoopSink()

    sinks: list[CRMSink] = []
    kinds = list(integ.crm_sinks)

    if "ghl" in kinds:
        if not integ.ghl_api_token:
            raise RuntimeError(
                f"business {getattr(business, 'id', '?')}: 'ghl' in "
                f"crm_sinks but ghl_api_token is not set"
            )
        if not integ.ghl_location_id:
            raise RuntimeError(
                f"business {getattr(business, 'id', '?')}: 'ghl' in "
                f"crm_sinks but ghl_location_id is not set"
            )
        from .ghl_client import GoHighLevelClient
        client = GoHighLevelClient(
            api_token=integ.ghl_api_token,
            location_id=integ.ghl_location_id,
            api_version=integ.ghl_api_version,
            default_calendar_id=integ.ghl_calendar_id,
        )
        sinks.append(GHLSink(client, business=business))

    if "hubspot" in kinds:
        if not integ.hubspot_access_token:
            raise RuntimeError(
                f"business {getattr(business, 'id', '?')}: 'hubspot' "
                f"in crm_sinks but hubspot_access_token is not set"
            )
        from .hubspot_client import HubSpotClient
        client = HubSpotClient(
            access_token=integ.hubspot_access_token,
            portal_id=integ.hubspot_portal_id,
            default_pipeline_id=integ.hubspot_pipeline_id,
            default_stage_id=integ.hubspot_stage_id,
            create_deals=integ.hubspot_create_deals,
        )
        sinks.append(HubSpotSink(client))

    if "webhook" in kinds:
        if not integ.webhook_url:
            raise RuntimeError(
                f"business {getattr(business, 'id', '?')}: 'webhook' "
                f"in crm_sinks but webhook_url is not set"
            )
        if not integ.webhook_hmac_secret:
            raise RuntimeError(
                f"business {getattr(business, 'id', '?')}: 'webhook' "
                f"in crm_sinks but webhook_hmac_secret is not set"
            )
        from .webhook_client import WebhookClient
        client = WebhookClient(
            url=integ.webhook_url,
            secret=integ.webhook_hmac_secret,
            source="voiceops-ai-agent",
        )
        sinks.append(WebhookSink(client))

    if "sheets" in kinds:
        if not integ.google_service_account_json:
            raise RuntimeError(
                f"business {getattr(business, 'id', '?')}: 'sheets' "
                f"in crm_sinks but google_service_account_json is "
                f"not set on tenant integrations"
            )
        # sheets needs a sheet id — not in Integrations yet. Fall back
        # to global settings if the field is added later. For now,
        # raise clearly so the operator adds the missing field.
        raise NotImplementedError(
            "sheets sink from business.integrations not yet wired — "
            "add integrations.google_sheet_id to Integrations schema "
            "when a tenant asks for it"
        )

    if not sinks:
        return NoopSink()
    if len(sinks) == 1:
        return sinks[0]
    return CompositeSink(sinks)

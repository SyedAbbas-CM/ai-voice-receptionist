"""Outbound dialer safety policy.

Ports the SubtoDealz n8n `Complete Filtration` JS filter to Python and adds
what that graph forgot: a DNC (do-not-call) list check.

Every outbound voice-agent client needs these three guards in this order:
    1. Are we inside the callee's business hours?
    2. Are we outside the per-lead cooldown window?
    3. Have we already burned the max attempts?
    4. Is the number on a DNC list?

Missing any of these is either impolite (calling at 3am) or illegal
(calling a DNC-listed number). This module packages the checks so every
route that dispatches outbound calls goes through one function.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Iterable, Optional
from zoneinfo import ZoneInfo


DEFAULT_BUSINESS_HOURS = ((time(9, 0), time(18, 0)),)  # 9am-6pm
DEFAULT_WEEKDAYS = (0, 1, 2, 3, 4)  # Mon-Fri (Python weekday: Mon=0)


@dataclass(frozen=True)
class DialerPolicy:
    """Tenant-configurable outbound-dialer policy.

    Defaults match SubtoDealz's original policy (Florida ET, 9am-6pm, Mon-Fri,
    24h cooldown, max 3 attempts) but every field is overridable per tenant."""

    timezone: str = "America/New_York"
    weekdays: tuple[int, ...] = DEFAULT_WEEKDAYS
    business_hours: tuple[tuple[time, time], ...] = DEFAULT_BUSINESS_HOURS
    cooldown_hours: int = 24
    max_attempts: int = 3
    dnc_numbers: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class Lead:
    """Normalized lead for the dialer. Field names align with the SubtoDealz
    Google Sheet columns so google_sheets.list_rows dicts map in cleanly."""

    phone: str
    name: str = ""
    total_calls: int = 0
    last_called: Optional[datetime] = None  # None if never called
    status: str = ""  # freeform status column from the sheet
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Decision:
    can_call: bool
    reason: str          # short machine-readable code
    detail: str = ""     # human-readable explanation


_TERMINAL_STATUSES = {"ended", "completed", "answered", "hot_lead", "do_not_call", "property_unavailable"}


def _normalize_phone(phone: str) -> str:
    """Loose phone normalization: strip everything except digits and leading +."""
    if not phone:
        return ""
    phone = phone.strip()
    plus = phone.startswith("+")
    digits = "".join(ch for ch in phone if ch.isdigit())
    return ("+" + digits) if plus else digits


def _within_business_hours(now_local: datetime, policy: DialerPolicy) -> bool:
    if now_local.weekday() not in policy.weekdays:
        return False
    now_t = now_local.time().replace(microsecond=0)
    return any(start <= now_t < end for start, end in policy.business_hours)


def decide_can_call(lead: Lead, policy: DialerPolicy, now_utc: Optional[datetime] = None) -> Decision:
    """The one function every outbound endpoint should call before dispatching.

    Returns a Decision(can_call, reason, detail). Never raises."""
    now_utc = now_utc or datetime.now(tz=ZoneInfo("UTC"))
    now_local = now_utc.astimezone(ZoneInfo(policy.timezone))

    if not lead.phone or not _normalize_phone(lead.phone):
        return Decision(False, "no_phone", "lead has no phone number")

    if _normalize_phone(lead.phone) in policy.dnc_numbers:
        return Decision(False, "dnc", "phone is on the do-not-call list")

    if lead.status and lead.status.strip().lower().replace(" ", "_") in _TERMINAL_STATUSES:
        return Decision(False, "terminal_status", f"lead status is terminal: {lead.status}")

    if lead.total_calls >= policy.max_attempts:
        return Decision(
            False, "max_attempts",
            f"already called {lead.total_calls} times, max is {policy.max_attempts}",
        )

    if lead.last_called is not None:
        # If last_called is naive, assume UTC (safe default; the ingester should provide tz-aware)
        last_called = lead.last_called
        if last_called.tzinfo is None:
            last_called = last_called.replace(tzinfo=ZoneInfo("UTC"))
        elapsed = now_utc - last_called
        if elapsed < timedelta(hours=policy.cooldown_hours):
            hours_remaining = policy.cooldown_hours - (elapsed.total_seconds() / 3600)
            return Decision(
                False, "cooldown",
                f"last called {elapsed} ago; cooldown {policy.cooldown_hours}h "
                f"({hours_remaining:.1f}h remaining)",
            )

    if not _within_business_hours(now_local, policy):
        return Decision(
            False, "out_of_hours",
            f"{now_local.strftime('%A %H:%M')} {policy.timezone} is outside allowed window",
        )

    return Decision(True, "ok")


def filter_leads(
    leads: Iterable[Lead],
    policy: DialerPolicy,
    now_utc: Optional[datetime] = None,
) -> tuple[list[Lead], list[tuple[Lead, Decision]]]:
    """Batch helper: returns (dialable, skipped_with_reason)."""
    dialable: list[Lead] = []
    skipped: list[tuple[Lead, Decision]] = []
    for lead in leads:
        decision = decide_can_call(lead, policy, now_utc=now_utc)
        (dialable if decision.can_call else skipped).append(lead if decision.can_call else (lead, decision))
    return dialable, skipped


async def decide_can_call_with_consent(
    lead: Lead,
    policy: DialerPolicy,
    consent_provider=None,
    now_utc: Optional[datetime] = None,
) -> Decision:
    """Async wrapper that adds a TCPA consent lookup on top of the sync
    policy check. If `consent_provider` is None, skips the consent step
    entirely (equivalent to decide_can_call).

    Use this for any outbound path that dials US numbers with an AI voice.
    Failing the consent check is $500-$1500 per call under 47 USC § 227(b)."""
    decision = decide_can_call(lead, policy, now_utc=now_utc)
    if not decision.can_call:
        return decision
    if consent_provider is None:
        return decision
    record = await consent_provider.has_consent(lead.phone)
    if not record.is_current:
        return Decision(
            False, "no_consent",
            f"no valid TCPA consent record for {lead.phone}; would be $500-1500/call in damages",
        )
    return decision


async def filter_leads_with_consent(
    leads: Iterable[Lead],
    policy: DialerPolicy,
    consent_provider=None,
    now_utc: Optional[datetime] = None,
) -> tuple[list[Lead], list[tuple[Lead, Decision]]]:
    """Async version of filter_leads that also checks TCPA consent."""
    dialable: list[Lead] = []
    skipped: list[tuple[Lead, Decision]] = []
    for lead in leads:
        d = await decide_can_call_with_consent(lead, policy, consent_provider, now_utc)
        if d.can_call:
            dialable.append(lead)
        else:
            skipped.append((lead, d))
    return dialable, skipped


def lead_from_sheet_row(row: dict) -> Lead:
    """Convert a Google Sheet row dict (as produced by google_sheets.list_rows)
    into a Lead. Tolerant of casing and missing columns — the SubtoDealz sheet
    had inconsistent casing on 'Total Calls' vs 'Total calls'."""
    def get(*candidates: str, default=None):
        for key in candidates:
            if key in row and row[key] not in (None, ""):
                return row[key]
        return default

    total_calls_raw = get("Total Calls", "Total calls", "total_calls", default=0)
    try:
        total_calls = int(str(total_calls_raw).strip() or 0)
    except (TypeError, ValueError):
        total_calls = 0

    last_called_raw = get("Last Called", "last_called", "Last called", default=None)
    last_called: Optional[datetime] = None
    if last_called_raw:
        try:
            last_called = datetime.fromisoformat(str(last_called_raw).replace("Z", "+00:00"))
        except ValueError:
            last_called = None

    return Lead(
        phone=str(get("Phone", "phone", "Phone Number", default="") or ""),
        name=str(get("Name", "name", default="") or ""),
        total_calls=total_calls,
        last_called=last_called,
        status=str(get("Status", "status", "lead_status", default="") or ""),
        extra=row,
    )

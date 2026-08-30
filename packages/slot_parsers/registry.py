"""Slot-type → (normalizer, validator) plugin registry.

R3 P0 (2026-08-13): the actor asks the registry for the pieces of ONE
slot type, then hands them to a `StructuredInputSession`.  Adding
`email`, `date`, `name`, etc. later means writing a new validator +
registering it here.  The actor never changes.

Currently registered:
  "phone" — Layer A: normalize_spoken_digits; Layer B: phonenumbers wrap
"""
from __future__ import annotations

from typing import Callable, Optional

from .phone import normalize_spoken_digits
from .phone_validator import PhoneStatus, parse_phone
from .session import SlotResult, SlotStatus, Validator


# ── phone validator adapter ──────────────────────────────────────────

def _phone_validator(canonical: str, config: dict) -> SlotResult:
    """Adapter: takes canonical digit string + config; returns SlotResult."""
    default_region = config.get("phone_default_region", "US")
    accepted_regions = config.get(
        "phone_accepted_regions",
        [default_region] if default_region else None,
    )

    if not canonical or canonical.strip("+") == "":
        return SlotResult(
            status=SlotStatus.INCOMPLETE, raw_digits=canonical,
        )

    r = parse_phone(canonical, default_region, accepted_regions)

    if r.status == PhoneStatus.COMPLETE:
        return SlotResult(
            status=SlotStatus.VALID,
            value=r.value,
            matched_region=r.matched_region,
            raw_digits=r.raw_digits,
        )
    if r.status == PhoneStatus.POSSIBLE:
        # Accept-with-confirmation for booking flows; the actor can
        # decide whether to prompt "just to confirm, is that ...?"
        return SlotResult(
            status=SlotStatus.POSSIBLE,
            value=r.value,
            matched_region=r.matched_region,
            reason=r.reason,
            raw_digits=r.raw_digits,
        )
    if r.status == PhoneStatus.PARTIAL:
        return SlotResult(
            status=SlotStatus.INCOMPLETE,
            reason=r.reason,
            raw_digits=r.raw_digits,
        )
    # TOO_LONG or INVALID or EMPTY → same INVALID status upstream;
    # reason field carries the detail.
    return SlotResult(
        status=SlotStatus.INVALID,
        reason=r.reason,
        raw_digits=r.raw_digits,
    )


# ── task #142 additions (2026-08-30) ────────────────────────────────
# email / name / date / yes_no — LK sub-agent pattern parity.
# Each slot has its own normalize + validator pair; the wider actor
# stays slot-type-agnostic.

from .email_slot import normalize_email, email_validator
from .name_slot import normalize_name, name_validator
from .date_slot import normalize_date, date_validator
from .yes_no_slot import normalize_yes_no, yes_no_validator


# ── registry ─────────────────────────────────────────────────────────

# Each entry maps slot_type → (normalize function, validator function).
_REGISTRY: dict[str, tuple[Optional[Callable[[str], str]], Validator]] = {
    "phone": (normalize_spoken_digits, _phone_validator),
    "email": (normalize_email, email_validator),
    "name": (normalize_name, name_validator),
    "date": (normalize_date, date_validator),
    "yes_no": (normalize_yes_no, yes_no_validator),
}


def get_slot_handlers(slot_type: str) -> tuple[Optional[Callable[[str], str]], Validator]:
    """Look up (normalizer, validator) for a slot type."""
    if slot_type not in _REGISTRY:
        raise ValueError(
            f"unknown slot type {slot_type!r}; registered: {sorted(_REGISTRY)}"
        )
    return _REGISTRY[slot_type]


def register_slot_type(
    slot_type: str,
    normalizer: Optional[Callable[[str], str]],
    validator: Validator,
) -> None:
    """Register a new slot type at import time (e.g. email, date, name).

    Tests can call this to override for test fixtures.
    """
    _REGISTRY[slot_type] = (normalizer, validator)


def list_slot_types() -> list[str]:
    return sorted(_REGISTRY)

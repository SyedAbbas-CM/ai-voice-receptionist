"""Generic structured-input engine for voice agents.

R3 P0 (2026-08-13): the actor doesn't know a phone is 10 or 11 digits.
The actor asks the registry for a slot type's handlers, creates a
`StructuredInputSession`, feeds it every STT final / DTMF event /
ANI lookup, and reads `session.result()` to decide what to say next.

Public API:
    from packages.slot_parsers import (
        StructuredInputSession, SlotStatus, SlotSource, SlotResult,
        get_slot_handlers, register_slot_type, list_slot_types,
        normalize_spoken_digits,          # Layer A (phone-shaped slots)
        parse_phone, PhoneStatus,         # Layer B for direct callers
    )

Example (phone slot for a US tenant that also accepts PK callers):
    normalizer, validator = get_slot_handlers("phone")
    session = StructuredInputSession(
        slot_type="phone",
        validator=validator,
        config={
            "phone_default_region": "US",
            "phone_accepted_regions": ["US", "PK"],
        },
    )

    result = session.feed("zero double three", normalize=normalizer)
    # -> SlotResult(status=INCOMPLETE, ...)

    result = session.feed("5244 7724", normalize=normalizer)
    # -> SlotResult(status=VALID, value="+923352447724", ...)

    session.commit(result.value)
"""
from .phone import normalize_spoken_digits
from .phone_validator import PhoneParseResult, PhoneStatus, parse_phone
from .registry import (
    get_slot_handlers,
    list_slot_types,
    register_slot_type,
)
from .session import (
    SlotFragment,
    SlotResult,
    SlotSource,
    SlotStatus,
    StructuredInputSession,
    Validator,
)

__all__ = [
    "SlotFragment",
    "SlotResult",
    "SlotSource",
    "SlotStatus",
    "StructuredInputSession",
    "Validator",
    "get_slot_handlers",
    "list_slot_types",
    "register_slot_type",
    "normalize_spoken_digits",
    "parse_phone",
    "PhoneParseResult",
    "PhoneStatus",
]

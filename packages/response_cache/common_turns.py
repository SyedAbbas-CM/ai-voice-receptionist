"""Boot-time seed lists for the response cache.

2026-08-19: the response cache was tracked as broken by observation
("never hits") — but it was actually just cold.  It only accumulates
entries when the LLM answers a NON-tool question, then a SECOND caller
asks it verbatim.  For a demo / first-caller experience, that's zero
hits.

This module ships per-vertical Q→A pairs harvested from BusinessProfile
FAQs + the top handful of client-asked questions ("do you take Delta
Dental?", "what are your hours?", "where are you?").  On boot the
warmup hook writes each pair into the response cache AND pre-generates
its TTS audio into the disk cache.  Result: those turns bypass the
LLM entirely and hit the disk in ~10ms + TTS wire in ~250ms.

Pairs are conservative — every one must be:
  1. Answerable purely from BusinessProfile (never inject invented facts)
  2. Not tool-dependent (booking / availability / lookup_faq never seeded)
  3. Phrased in a way that survives `normalize_input` — no dates, no
     names, no numbers that vary caller-to-caller

For unknown verticals we ship an empty list — the cache still fills
naturally as the LLM answers repeat questions.
"""
from __future__ import annotations

from typing import Sequence

from packages.schemas import BusinessProfile


def _clinic_pairs(business: BusinessProfile) -> list[tuple[str, str]]:
    """Common clinic-vertical FAQ turns.  Each item is
    (list-of-input-variants, canonical-reply).  We seed EVERY variant
    against the same reply so slightly different phrasings all hit.

    Reply text is pulled from business.faqs where possible so per-tenant
    branding shows up automatically.  For pairs with no matching FAQ
    key we fall back to a safe generic + the business profile fields
    (address, phone) so we never invent a fact."""
    faqs = business.faqs or {}
    pairs: list[tuple[str, str]] = []

    def _seed(inputs: Sequence[str], reply: str) -> None:
        if not reply:
            return
        for inp in inputs:
            pairs.append((inp, reply))

    # Insurance
    _seed(
        [
            "do you take delta dental",
            "do you accept delta dental",
            "is delta dental accepted",
            "what insurance do you take",
            "what insurance do you accept",
            "which insurance do you accept",
            "do you take insurance",
            "do you accept insurance",
        ],
        faqs.get("insurance", ""),
    )

    # Hours
    _seed(
        [
            "what are your hours",
            "what time do you open",
            "what time do you close",
            "when are you open",
            "when do you open",
            "are you open on saturday",
            "are you open on sunday",
            "are you open today",
            "are you open tomorrow",
            "what are the hours",
        ],
        faqs.get("hours", ""),
    )

    # Location / address
    address_reply = faqs.get("location", "") or (
        f"We're at {business.address}." if business.address else ""
    )
    _seed(
        [
            "where are you located",
            "where are you",
            "what's your address",
            "what is your address",
            "where's the office",
            "where is the office",
            "how do i get there",
            "what's the address",
        ],
        address_reply,
    )

    # Parking
    _seed(
        [
            "is there parking",
            "do you have parking",
            "where do i park",
            "is parking free",
        ],
        faqs.get("parking", ""),
    )

    # Cancellation
    _seed(
        [
            "what's your cancellation policy",
            "what is your cancellation policy",
            "how do i cancel",
            "can i cancel",
            "cancellation fee",
        ],
        faqs.get("cancellation", ""),
    )

    # Kids / pediatric
    _seed(
        [
            "do you see kids",
            "do you see children",
            "is there a pediatric dentist",
            "do you treat children",
        ],
        faqs.get("kids", ""),
    )

    # Payment plans
    _seed(
        [
            "do you have payment plans",
            "do you offer payment plans",
            "do you accept care credit",
            "do you take care credit",
            "can i pay in installments",
        ],
        faqs.get("payment plans", ""),
    )

    # Invisalign
    _seed(
        [
            "do you do invisalign",
            "do you offer invisalign",
            "how much is invisalign",
            "how much does invisalign cost",
        ],
        faqs.get("invisalign", ""),
    )

    # New patient
    _seed(
        [
            "i'm a new patient",
            "i am a new patient",
            "i'm new",
            "what do new patients need to bring",
            "what should i bring as a new patient",
        ],
        faqs.get("new patient", ""),
    )

    # Emergency
    _seed(
        [
            "i have a dental emergency",
            "i have an emergency",
            "is there someone after hours",
            "who do i call after hours",
        ],
        faqs.get("emergency", ""),
    )

    # Spanish
    _seed(
        [
            "do you speak spanish",
            "hablas espanol",
            "is there anyone who speaks spanish",
        ],
        faqs.get("spanish", ""),
    )

    # Drop any pair whose reply came back empty (no FAQ, no fallback).
    return [(inp, reply) for inp, reply in pairs if reply.strip()]


_VERTICAL_BUILDERS = {
    "clinic": _clinic_pairs,
    "dentist": _clinic_pairs,
    "dental": _clinic_pairs,
}


def common_turns_for(business: BusinessProfile) -> list[tuple[str, str]]:
    """Return (input, reply) pairs to seed into the response cache at
    boot for `business`.  Returns [] for unknown verticals — the cache
    still fills naturally as the LLM answers repeats."""
    vertical = (getattr(business, "vertical", None) or "").lower()
    builder = _VERTICAL_BUILDERS.get(vertical)
    if builder is None:
        return []
    return builder(business)

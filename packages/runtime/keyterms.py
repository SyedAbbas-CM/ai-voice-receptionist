"""Tenant-aware STT keyterm boosting.

## What this solves

Deepgram (Nova-3 + Flux) supports the `keyterm` query parameter: repeated
tokens the recognizer will *prefer* when acoustic evidence is ambiguous.
Historically we sent a hardcoded dental term list from
`app.providers.stt.deepgram_stt._DENTAL_KEYTERMS`. That worked for a
single-vertical demo but silently regressed the moment a caller said the
business name ("Smile Dental" → "Seth Muhammadabas" on CAa8d209...) or
mentioned a tenant-specific service.

## LK steal T3 (from LiveKit Agents)

LK's `voice/keyterm_detection.py` exposes `STTContextOptions.keyterms:
list[str]` — wire keyterms per-session from tenant context. We adopt the
same pattern but WITHOUT LK's dynamic-detection background loop (queued
as a separate task; complexity > value at pilot).

## Contract

`compute_keyterms(business, extras=None) -> list[str]`

Returns a deduped, ordered list ready to drop into the Deepgram query
params via repeated `keyterm=X` pairs. Ordering is stable so cached
Deepgram query URLs (if any downstream caching happens) don't churn on
every request.

Priority order (most important first, since Deepgram weights early
terms more strongly at request-boundary approximation):
  1. Business name variants (full + first word)
  2. Service names (verbatim, plus tokenized single words for e.g.
     "Follow-up visit" → also boost "follow-up" alone)
  3. Common receptionist vocabulary (appointment, reschedule, cancel...)
  4. Vertical-specific fallback set (dental/medical/real-estate/etc.)
  5. Caller-provided extras (e.g. staff names loaded from a per-tenant
     side-channel — currently unused, hook exists for future)

## Non-goals for v1

- Dynamic keyterm detection from live transcripts (LK does this via
  `keyterm_detection.py` with a `_PENDING_TTL` gate; deferred).
- Multi-language keyterm sets (Deepgram accepts UTF-8 in all supported
  languages; the caller's job to pass appropriately-cased terms).
- STAFF names from a separate DB table — schema hook is `extras=`;
  wire-up when the staff table lands.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional


# ─── vertical fallback sets ────────────────────────────────────────────────
#
# These are the "safety net" — used verbatim when the tenant's
# BusinessProfile.services is thin, so we still boost obvious domain
# vocab. Ordered roughly by frequency in real dental/medical calls.
# Extending: keep additions lowercase; matching is case-insensitive but
# Deepgram sends them back in the STT output using the exact case we
# provided, so lowercase avoids surprising the LLM's downstream matching.

_VERTICAL_KEYTERMS: dict[str, tuple[str, ...]] = {
    "clinic": (
        # Dental / medical services
        "implant", "implants", "tooth implant", "dental implant",
        # 2026-08-31 CALL-BUG-06 followup: user reported "cleaning"
        # misheard as "greeting" on Flux. Boost the exact word + a
        # phrase variant so Deepgram biases toward the dental sense.
        "cleaning", "a cleaning", "dental cleaning", "teeth cleaning",
        # 2026-08-31 CALL-BUG-13: staff surnames from the sample clinic
        # fixture (Whitfield → misheard as "upgrade field"; Chen ok).
        # In production this list should be per-tenant from the business
        # profile's staff.  For now, boost the fixture names.
        "Whitfield", "Doctor Whitfield", "Chen", "Doctor Chen",
        # 2026-08-31 CALL-BUG-17: common caller names our test caller
        # (South Asian / Muslim) uses that Flux keeps mangling
        # ("Abbas" → "a boss", "Syed" → "Seth", "Mohammad" → "mom on
        # the bus"). Same per-tenant caller-name-hints future work,
        # but boost the operator's own name here so demo calls work.
        "Abbas", "Syed", "Mohammad", "Muhammad", "Ahmed", "Ali",
        "my name is Abbas", "my name is Syed",
        "prophy", "prophylaxis",
        "crown", "crowns", "bridge", "veneer", "veneers",
        "root canal", "endodontic", "endo",
        "extraction", "wisdom tooth", "wisdom teeth",
        "filling", "cavity", "cavities",
        "orthodontics", "braces", "invisalign",
        "periodontal", "gum disease", "gingivitis",
        "whitening", "bleaching",
        "denture", "dentures",
        "x-ray", "x-rays", "panoramic",
        # Receptionist vocab
        "consultation", "checkup", "exam", "hygienist",
        "appointment", "reschedule", "cancel", "follow-up",
        "insurance", "copay", "deductible",
    ),
    "restaurant": (
        "reservation", "booking", "party", "waitlist",
        "allergy", "gluten-free", "vegan", "vegetarian",
        "cancel", "reschedule",
    ),
    "real-estate": (
        # For Sofia/Ribeira Prime — Portuguese real-estate context
        "viewing", "showing", "tour", "listing", "apartment",
        "condo", "villa", "duplex", "penthouse", "studio",
        "offer", "closing", "mortgage",
    ),
    # Add more verticals here as we onboard them.
}


# Deepgram documents `keyterm` accepting "any string of any length" but
# empirically very long terms (>~50 chars) waste the boost budget. Also
# a single request cap somewhere in the 200-term range. Keep our output
# under both:
_MAX_TERMS_PER_REQUEST = 128
_MAX_TERM_LEN_CHARS = 48


def compute_keyterms(
    business: object,
    extras: Optional[Iterable[str]] = None,
    include_vertical_fallback: bool = True,
) -> list[str]:
    """Compute tenant-aware keyterm boost list for Deepgram STT.

    `business` is a `packages.schemas.business.BusinessProfile` — kept
    as `object` in the signature to avoid a circular import (the schemas
    package should not need to import runtime).

    `extras` optional caller-provided terms (staff names, campaign
    keywords, etc.) — highest priority after business identity.

    `include_vertical_fallback` set False to disable the safety-net set,
    useful for tests or extremely narrow-vocab tenants who WANT the
    recognizer unbiased on the vertical's general vocabulary.

    Returns: ordered, deduped, capped list ready to feed into repeated
    Deepgram `keyterm=` query params.
    """
    seen: set[str] = set()
    out: list[str] = []

    def _add(term: object) -> None:
        # Defensive: any None / non-string / too-long / duplicate is dropped
        # silently. Callers should NOT need to sanitize before us.
        if not term:
            return
        s = str(term).strip()
        if not s:
            return
        if len(s) > _MAX_TERM_LEN_CHARS:
            # Truncate word-wise rather than mid-word — usually catches
            # a compound service name like "Full-mouth periodontal
            # deep-scaling and root-planing" that would otherwise get
            # sliced. Better to drop everything after the cap than emit
            # a broken token.
            return
        key = s.lower()
        if key in seen:
            return
        seen.add(key)
        out.append(s)

    # ─── 1. Business name variants ──
    # The single biggest keyterm win. "Smile Dental" transcribing as
    # "Seth Muhammadabas" (real bug on CAa8d209) is exactly what this
    # boost fixes.
    name = getattr(business, "name", None)
    if name:
        _add(name)
        # Also boost the first word alone — receptionists often
        # abbreviate ("Thanks for calling Smile...") and callers do too.
        first_word = str(name).split()[0] if str(name).split() else ""
        if first_word and len(first_word) > 2:  # skip "A", "El", etc.
            _add(first_word)

    # ─── 2. Extras (staff names, campaign terms) ──
    # High priority: these are usually per-caller-conversation specific
    # and highest-signal. Empty by default; hook for future.
    if extras:
        for term in extras:
            _add(term)

    # ─── 3. Service names (verbatim + tokenized) ──
    # "Follow-up visit" boosts both "Follow-up visit" AND "follow-up"
    # so a caller saying just "follow-up" also matches. Multi-word
    # services get their leading token boosted too.
    services = getattr(business, "services", None) or []
    for svc in services:
        svc_name = getattr(svc, "name", None) or ""
        if not svc_name:
            continue
        _add(svc_name)
        # Tokenize on whitespace + hyphens to catch subword matches.
        # "Root canal" → boost "canal" too; "Deep-cleaning" → boost
        # "cleaning" too. Skip stopwords + tokens < 4 chars.
        tokens = re.split(r"[\s\-/]+", svc_name)
        for token in tokens:
            if len(token) >= 4 and token.lower() not in _STOPWORDS:
                _add(token)

    # ─── 4. Vertical fallback set ──
    if include_vertical_fallback:
        vertical = getattr(business, "vertical", "clinic") or "clinic"
        for term in _VERTICAL_KEYTERMS.get(vertical, ()):
            _add(term)

    # ─── 5. Cap total count ──
    # Deepgram documents "many keyterms are supported" but doesn't
    # publish an exact limit. 128 is a defensive cap; if we ever hit
    # it we're probably keyterm-spamming (which dilutes each term's
    # weight) — worth an audit rather than silently expanding.
    if len(out) > _MAX_TERMS_PER_REQUEST:
        out = out[:_MAX_TERMS_PER_REQUEST]

    return out


# Words we never boost on their own — too generic, wastes budget.
_STOPWORDS: frozenset[str] = frozenset({
    "the", "a", "an", "and", "or", "for", "with", "without",
    "your", "our", "my", "you", "we", "us", "me",
    "of", "in", "on", "at", "by", "to", "from",
    "is", "are", "was", "were", "be", "been", "being",
    "visit", "service", "type", "kind",
})

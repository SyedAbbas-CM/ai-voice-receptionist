"""Service-name alias resolver — canonicalizes caller-spoken service
names to the tenant's actual `BusinessProfile.services` entries.

2026-08-29 (BUG-CHR-03): Christiaan said 'A follow-up' as his service.
The clinic fixture has no service called 'follow-up' or 'follow up' —
its closest match is 'Emergency exam' (for urgent visits) or 'New
patient exam' (for first-timers).  LLM got the ambiguous input, had
no clear rule for 'caller said a service I don't have,' and returned
an empty completion.

This resolver takes the raw caller-spoken service string + the
tenant's actual services list and returns:
  - MATCH_EXACT: found an exact or canonical alias match → pass to tool
  - MATCH_FUZZY: found a plausible match with confidence 0.6-0.9 →
                 confirm with caller ("did you mean X?")
  - AMBIGUOUS: caller's phrase maps to 2+ tenant services with
               similar confidence → ask them to clarify
  - UNKNOWN: no plausible match → ask what they need instead of
             stalling

The prompt reads this contract via a new tool-facing schema (see
BUG-CHR-03 prompt update) so gpt-4o-mini has explicit rules for each
outcome — no more empty completions on ambiguous service names.
"""
from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import Enum
from typing import Optional


# 2026-08-31 (CALL-BUG-06): tokens that appear inside common service
# names ("New patient exam WITH X-rays", "Cleaning AND recall exam")
# but carry zero service-intent signal. Never award the fuzzy
# token-overlap bonus for these. Real trace: caller said "Hello. am
# I talking with" (hearing check) → "with" matched "with" in service
# name → confidence 0.60 → wrong service persisted for entire call.
_TOKEN_STOPWORDS: frozenset[str] = frozenset({
    # Prepositions / conjunctions / pronouns
    "with", "without", "and", "or", "for", "the", "your", "our",
    "have", "want", "would", "could", "like", "need", "book",
    "make", "get", "put", "set", "call", "back",
    # Utterance fillers
    "hello", "hey", "yeah", "okay", "sure", "please", "thanks",
    "just", "well", "know", "sorry",
    # STT-noise / hearing-check patterns
    "talking", "hear", "there", "listen", "listening",
    # Overly-generic connectives that appear in service names
    "visit", "appointment", "session", "consultation",
})


class ServiceMatchKind(str, Enum):
    """Outcome of an alias lookup."""
    MATCH_EXACT = "match_exact"    # confident: use verbatim
    MATCH_FUZZY = "match_fuzzy"    # plausible: confirm with caller first
    AMBIGUOUS = "ambiguous"        # 2+ plausible: ask to clarify
    UNKNOWN = "unknown"            # no match: ask what they need


@dataclass(frozen=True)
class ServiceMatch:
    """Result of a service-alias resolution.

    - kind: which outcome branch fired
    - canonical_name: the exact tenant service name (from
      BusinessProfile.services[].name).  None when UNKNOWN.
    - candidates: for AMBIGUOUS, the 2-3 canonical names the caller
      might have meant.  Empty otherwise.
    - confidence: 0.0-1.0 similarity score.  EXACT is always 1.0.
    - reason: short human-readable explanation for logs.
    """
    kind: ServiceMatchKind
    canonical_name: Optional[str] = None
    candidates: tuple[str, ...] = ()
    confidence: float = 0.0
    reason: str = ""


# ── static alias map ──────────────────────────────────────────────
#
# Maps caller-spoken variants to a set of tenant service KEYWORDS.
# Resolver then matches those keywords against the actual tenant
# service names (which vary per business).
#
# LEFT: what a caller might say (lowercased, minimally punctuated)
# RIGHT: tuple of keyword tokens that should appear in the tenant
#        service name for the match to fire

_CALLER_TO_KEYWORDS: dict[str, tuple[str, ...]] = {
    # Cleaning aliases
    "cleaning":           ("cleaning",),
    "a cleaning":         ("cleaning",),
    "a clean":            ("cleaning",),
    "just a cleaning":    ("cleaning",),
    "regular cleaning":   ("cleaning",),
    "teeth cleaning":     ("cleaning",),
    "prophy":             ("cleaning",),   # dental prophylaxis
    "hygiene":            ("cleaning",),
    # Check-up / exam aliases
    "check up":           ("exam",),
    "checkup":            ("exam",),
    "check-up":           ("exam",),
    "regular check up":   ("exam",),
    "routine check up":   ("exam",),
    "exam":               ("exam",),
    "examination":        ("exam",),
    "new patient exam":   ("new patient", "exam"),
    "new patient":        ("new patient",),
    "first visit":        ("new patient", "pediatric first"),
    # Filling / cavity aliases
    "filling":            ("filling",),
    "cavity":             ("filling",),
    "cavities":           ("filling",),
    # Emergency aliases
    "emergency":          ("emergency",),
    "emergency exam":     ("emergency",),
    "urgent":             ("emergency",),
    "toothache":          ("emergency",),
    "pain":               ("emergency",),
    # Whitening
    "whitening":          ("whitening",),
    "zoom whitening":     ("zoom", "whitening"),
    "teeth whitening":    ("whitening",),
    # Consultations
    "invisalign":         ("invisalign",),
    "braces":             ("invisalign",),
    "implant":            ("implant",),
    "implants":           ("implant",),
    "consultation":       ("consultation",),
    "consult":            ("consultation",),
    # Follow-up — the exact Christiaan trigger.  Now that we added
    # 'Follow-up visit' as a real fixture service (2026-08-29), map
    # directly.  For tenants that DON'T have a follow-up service
    # configured, the resolver falls through to fuzzy matching which
    # will pick their nearest analog (typically 'Recall exam' or
    # 'Emergency exam').
    "follow up":          ("follow-up",),
    "follow-up":          ("follow-up",),
    "a follow up":        ("follow-up",),
    "a follow-up":        ("follow-up",),
    "return visit":       ("follow-up",),
    "recheck":            ("follow-up",),
    "second visit":       ("follow-up",),
    "recall":             ("recall",),   # 6-month standing appointment
    "six month":          ("recall",),
    "6 month":            ("recall",),
    # Kid-related — expat common phrasings included so 'my kids' / 'the
    # kids' route without needing possessive-strip logic in _normalize.
    "kids":               ("pediatric",),
    "my kids":            ("pediatric",),
    "the kids":           ("pediatric",),
    "child":              ("pediatric",),
    "my child":           ("pediatric",),
    "my son":             ("pediatric",),
    "my daughter":        ("pediatric",),
    "pediatric":          ("pediatric",),
    # Real-estate flavors (for RealEstateToolHandler tenants)
    "viewing":            ("viewing",),
    "property viewing":   ("viewing",),
    "see the property":   ("viewing",),
    "valuation":          ("valuation",),
    "home valuation":     ("valuation",),
    "rental":             ("rental",),
    "rental enquiry":     ("rental",),
    # Restaurant flavors (for RestaurantToolHandler tenants)
    "reservation":        ("table", "reservation"),
    "table":              ("table",),
    "book a table":       ("table",),
}


def _normalize(s: str) -> str:
    """Lowercase, strip common punctuation + collapse whitespace."""
    if not s:
        return ""
    out = s.lower().strip()
    # Strip leading articles.
    for prefix in ("a ", "an ", "the ", "just a ", "just an "):
        if out.startswith(prefix):
            out = out[len(prefix):]
    # Collapse whitespace.
    out = " ".join(out.split())
    return out


def _similarity(a: str, b: str) -> float:
    """Simple string similarity 0.0-1.0."""
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def resolve_service(
    spoken: str,
    tenant_services: list,   # list of ServiceOffering or dicts with 'name'
) -> ServiceMatch:
    """Resolve a caller-spoken service phrase to a tenant service.

    Never raises.  Malformed input → UNKNOWN.
    """
    try:
        if not spoken:
            return ServiceMatch(
                kind=ServiceMatchKind.UNKNOWN,
                reason="empty spoken input",
            )
        if not tenant_services:
            return ServiceMatch(
                kind=ServiceMatchKind.UNKNOWN,
                reason="tenant has no services configured",
            )
        # Normalize inputs.
        norm_spoken = _normalize(spoken)
        # Extract canonical names.
        service_names: list[str] = []
        for s in tenant_services:
            name = getattr(s, "name", None) if not isinstance(s, dict) else s.get("name")
            if name:
                service_names.append(str(name))
        if not service_names:
            return ServiceMatch(
                kind=ServiceMatchKind.UNKNOWN,
                reason="tenant services have no names",
            )
        # 1. Exact-name match (case-insensitive).
        for name in service_names:
            if _normalize(name) == norm_spoken:
                return ServiceMatch(
                    kind=ServiceMatchKind.MATCH_EXACT,
                    canonical_name=name,
                    confidence=1.0,
                    reason="exact name match",
                )
        # 2. Alias-keyword match.  Find tenant services whose names
        #    contain the required keywords for this alias.
        keywords = _CALLER_TO_KEYWORDS.get(norm_spoken)
        if keywords:
            matches: list[str] = []
            for name in service_names:
                nlow = name.lower()
                if all(k in nlow for k in keywords):
                    matches.append(name)
            if len(matches) == 1:
                return ServiceMatch(
                    kind=ServiceMatchKind.MATCH_EXACT,
                    canonical_name=matches[0],
                    confidence=0.95,
                    reason=f"alias '{norm_spoken}' → keywords {keywords}",
                )
            if len(matches) >= 2:
                # Multiple tenant services matched → ambiguous.
                return ServiceMatch(
                    kind=ServiceMatchKind.AMBIGUOUS,
                    candidates=tuple(matches[:3]),
                    confidence=0.7,
                    reason=(
                        f"alias '{norm_spoken}' matched "
                        f"{len(matches)} tenant services"
                    ),
                )
        # 3. Fuzzy substring / similarity fallback.  Rank all services
        #    by similarity to the normalized spoken phrase.
        ranked: list[tuple[str, float]] = []
        for name in service_names:
            sim = max(
                _similarity(norm_spoken, name),
                _similarity(norm_spoken, _normalize(name)),
            )
            # Bonus if any token of the spoken phrase appears in the
            # service name.
            # 2026-08-31 (CALL-BUG-06 fix): stopword-guarded. Real trace
            # from CAbd671430f1297c1bbe0640a977060f1f: caller said
            # "Hello. am I talking with" (a hearing check) — token
            # "with" (4 chars) appeared in "New patient exam with
            # X-rays" → fuzzy conf=0.60 → MATCH_FUZZY → wrong service
            # got persisted in _collected_slots, agent asked wrong
            # questions for 8 subsequent turns. Filler / stopword
            # tokens must never trigger a service match on their own.
            for tok in norm_spoken.split():
                if len(tok) >= 4 and tok not in _TOKEN_STOPWORDS \
                        and tok in name.lower():
                    sim = max(sim, 0.6)
            ranked.append((name, sim))
        ranked.sort(key=lambda x: x[1], reverse=True)
        top_name, top_score = ranked[0]
        if top_score >= 0.9:
            return ServiceMatch(
                kind=ServiceMatchKind.MATCH_EXACT,
                canonical_name=top_name,
                confidence=top_score,
                reason="high-similarity fuzzy match",
            )
        if top_score >= 0.6:
            # Check if there's a second candidate with similar score.
            if (
                len(ranked) >= 2
                and ranked[1][1] >= top_score - 0.1
            ):
                # Ambiguous — two similar candidates.
                candidates = tuple(
                    n for n, s in ranked[:3] if s >= 0.55
                )
                return ServiceMatch(
                    kind=ServiceMatchKind.AMBIGUOUS,
                    candidates=candidates,
                    confidence=top_score,
                    reason="multiple similar fuzzy matches",
                )
            return ServiceMatch(
                kind=ServiceMatchKind.MATCH_FUZZY,
                canonical_name=top_name,
                confidence=top_score,
                reason="single plausible fuzzy match",
            )
        # No plausible match.
        return ServiceMatch(
            kind=ServiceMatchKind.UNKNOWN,
            confidence=top_score,
            reason=(
                f"no match (best candidate '{top_name}' at "
                f"{top_score:.2f})"
            ),
        )
    except Exception as e:
        # Defensive: never raise up to the tool handler.
        return ServiceMatch(
            kind=ServiceMatchKind.UNKNOWN,
            reason=f"resolver exception: {e}",
        )


__all__ = [
    "ServiceMatchKind",
    "ServiceMatch",
    "resolve_service",
]

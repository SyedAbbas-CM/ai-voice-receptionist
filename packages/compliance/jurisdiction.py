"""Jurisdictional compliance checks — call recording consent + AI disclosure.

**Scope:** United States, state-level wiretap + call-recording laws.
International tenants (Canada, EU, UK, Australia) have their own laws
that are not yet modeled here — the audit function surfaces "unknown
jurisdiction" so ops can add coverage manually.

**Two-party (all-party) consent states** — every party to the call
must be informed BEFORE the call is recorded.  For an inbound voice
agent, "informed" means the greeting explicitly says the call may be
recorded, spoken loud enough for the caller to hear before they answer
a substantive question.

Sources (checked 2026-08-25):
  - Digital Media Law Project state-by-state recording laws
  - RCFP (Reporters Committee for Freedom of the Press) recording chart
  - Actual state statutes cited below per state

**Rule of thumb:** If unsure whether a state is one-party or two-party,
treat it as two-party.  The cost of a compliant greeting is 1s of
audio.  The cost of a wiretap violation is 3-5 years jail + civil
damages per call recorded without consent.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)


# ── two-party (all-party) consent states ─────────────────────────────
#
# Kept as a set for O(1) lookup.  Ordered alphabetically in source
# because the linter yells at unsorted collections.
#
# CT + HI are contested/hybrid — CT requires two-party ONLY for phone
# recordings where BOTH parties are in CT; HI requires all-party ONLY
# when the recorder is a party (which is our case — we ARE recording,
# even if the "we" is an AI).  Treating both as two-party for safety.
TWO_PARTY_STATES: frozenset[str] = frozenset({
    "CA",  # California — Cal. Penal Code § 632
    "CT",  # Connecticut — Conn. Gen. Stat. § 52-570d (contested; treat safely)
    "FL",  # Florida — Fla. Stat. § 934.03
    "HI",  # Hawaii — Haw. Rev. Stat. § 803-42 (contested; treat safely)
    "IL",  # Illinois — 720 ILCS 5/14-2
    "MD",  # Maryland — Md. Code § 10-402
    "MA",  # Massachusetts — Mass. Gen. Laws ch. 272 § 99
    "MT",  # Montana — Mont. Code Ann. § 45-8-213
    "NV",  # Nevada — Nev. Rev. Stat. § 200.620 (federal one-party but state two-party for in-person)
    "NH",  # New Hampshire — N.H. Rev. Stat. § 570-A:2
    "PA",  # Pennsylvania — 18 Pa. Cons. Stat. § 5703
    "WA",  # Washington — Wash. Rev. Code § 9.73.030
})


# ── timezone → state guess ──────────────────────────────────────────
#
# BusinessProfile carries `timezone: str` (IANA name like America/Chicago).
# Timezone doesn't uniquely identify a state (America/Chicago covers
# IL, WI, MO, AR, LA, TN, KY, MS, AL) but it lets us WARN when the
# timezone is ONLY consistent with a two-party state.
#
# Kept minimal — only mappings where the tz is a strong signal.  For
# ambiguous tzs (America/Chicago) we ask for `business.address` instead.
_TZ_TO_STATE_HINTS: dict[str, tuple[str, ...]] = {
    "America/Los_Angeles":  ("CA",),                    # unambiguous — CA
    "America/Los_Angeles/Nevada": ("CA", "NV"),         # not a real tz; kept as doc
    "Pacific/Honolulu":      ("HI",),                    # unambiguous — HI
    "America/Anchorage":     ("AK",),
    "America/New_York":      ("NY", "PA", "FL", "MA"),  # ambiguous (some 2p)
    "America/Chicago":       ("IL", "MO", "TN", "AR"),   # ambiguous (IL is 2p; others 1p)
    "America/Denver":        ("MT", "CO", "NM", "WY"),   # ambiguous (MT is 2p; others 1p)
    "America/Boise":         ("MT",),                    # not tz-canonical but same latitude
    "America/Detroit":       ("MI",),
    "America/Indiana/Indianapolis": ("IN",),
    "America/Phoenix":       ("AZ",),
}


# US state 2-letter codes — for scanning `business.address` strings.
_US_STATE_CODES: frozenset[str] = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI",
    "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC",
    "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT",
    "VT", "VA", "WA", "WV", "WI", "WY", "DC",
})


_ADDRESS_STATE_RE = re.compile(
    r"\b([A-Z]{2})\s+\d{5}(?:-\d{4})?\b"  # "TX 75093" / "CA 94103-1234"
)


def infer_us_state(
    *, address: Optional[str] = None, timezone: Optional[str] = None,
) -> Optional[str]:
    """Best-effort US state inference from business profile fields.

    Returns None when we can't confidently identify the state.  Priority:
      1. address — scan for "<CODE> <ZIP>" pattern (most specific)
      2. timezone — only when the tz maps to a SINGLE state

    Not a hard identity check; the caller should treat None as
    "unknown, ask ops."
    """
    if address:
        m = _ADDRESS_STATE_RE.search(address)
        if m:
            code = m.group(1).upper()
            if code in _US_STATE_CODES:
                return code
    if timezone:
        hints = _TZ_TO_STATE_HINTS.get(timezone)
        if hints and len(hints) == 1:
            return hints[0]
    return None


@dataclass(frozen=True)
class ComplianceAudit:
    """Result of `audit_business_compliance`.

    - ok=True: no known compliance gaps for the profile as configured.
    - ok=False: at least one warning; `warnings` explains each.
    - Even ok=True carries `notes` for advisory info (e.g. "state
      unknown, cannot verify recording-consent requirement").
    """
    ok: bool
    inferred_state: Optional[str]
    warnings: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def format_human(self) -> str:
        lines = []
        st = self.inferred_state or "unknown"
        lines.append(f"Inferred state: {st}")
        if self.warnings:
            lines.append("Warnings:")
            for w in self.warnings:
                lines.append(f"  - {w}")
        if self.notes:
            lines.append("Notes:")
            for n in self.notes:
                lines.append(f"  - {n}")
        if self.ok and not self.warnings:
            lines.append("OK — no known compliance gaps.")
        return "\n".join(lines)


def audit_business_compliance(business) -> ComplianceAudit:
    """Audit a BusinessProfile for common US compliance gaps.

    Currently checks:
      - Two-party recording consent: if state is a two-party state
        AND `recording_notice_enabled=False`, flag WARNING.
      - AI/automation disclosure: recommended everywhere (FTC guidance
        + state chatbot disclosure laws in CA, UT).  Not a hard-fail
        anywhere yet.
      - Unknown jurisdiction: note it so ops can research + configure.

    Never raises.  Returns ComplianceAudit even on garbage input.
    """
    warnings: list[str] = []
    notes: list[str] = []
    inferred: Optional[str] = None
    try:
        address = getattr(business, "address", None)
        tz = getattr(business, "timezone", None)
        recording_enabled = bool(
            getattr(business, "recording_notice_enabled", False)
        )
        ai_disclosure_enabled = bool(
            getattr(business, "ai_disclosure_enabled", False)
        )
        inferred = infer_us_state(address=address, timezone=tz)

        if inferred is None:
            notes.append(
                "Could not infer US state from address/timezone. "
                "If serving a two-party-consent state (CA, FL, IL, MD, "
                "MA, MT, NV, NH, PA, WA, CT, HI), MANUALLY set "
                "recording_notice_enabled=True in the business profile."
            )
        elif inferred in TWO_PARTY_STATES:
            if not recording_enabled:
                warnings.append(
                    f"Business is in {inferred} (two-party consent state). "
                    f"recording_notice_enabled is FALSE — every call "
                    f"recorded without the greeting disclosure may "
                    f"violate state wiretap law. Set "
                    f"recording_notice_enabled=True in the profile."
                )
            else:
                notes.append(
                    f"Business is in {inferred} (two-party state); "
                    f"recording notice enabled ✓"
                )
        else:
            notes.append(
                f"Business is in {inferred} (one-party state); recording "
                f"consent notice is legally optional but still recommended."
            )

        if not ai_disclosure_enabled:
            notes.append(
                "ai_disclosure_enabled is FALSE. California SB 1001 and "
                "Utah AI Policy Act require chatbots to disclose they are "
                "automated when asked. Even outside those states, FTC "
                "guidance recommends disclosure. Consider setting True."
            )
    except Exception as e:
        # Never crash boot on a compliance-check bug — log + return
        # an empty-warnings audit so ops still sees a signal.
        notes.append(f"audit exception (non-fatal): {e}")
        return ComplianceAudit(
            ok=True, inferred_state=inferred,
            warnings=(), notes=tuple(notes),
        )
    return ComplianceAudit(
        ok=(not warnings),
        inferred_state=inferred,
        warnings=tuple(warnings),
        notes=tuple(notes),
    )


def log_compliance_audit(business, *, source: str = "boot") -> ComplianceAudit:
    """Convenience wrapper — audit + log at appropriate level.

    Called from app startup so warnings surface in the boot log where
    ops can see them, not silently.  Warnings log at WARNING level so
    they show up in log-aggregation dashboards.
    """
    audit = audit_business_compliance(business)
    header = f"COMPLIANCE_AUDIT source={source} state={audit.inferred_state or 'unknown'}"
    if audit.warnings:
        for w in audit.warnings:
            log.warning("%s WARN: %s", header, w)
    for n in audit.notes:
        log.info("%s NOTE: %s", header, n)
    if audit.ok and not audit.warnings:
        log.info("%s OK", header)
    return audit


__all__ = [
    "TWO_PARTY_STATES",
    "ComplianceAudit",
    "audit_business_compliance",
    "infer_us_state",
    "log_compliance_audit",
]

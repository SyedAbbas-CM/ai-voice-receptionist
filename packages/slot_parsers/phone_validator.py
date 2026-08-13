"""Phone slot: Layer B (semantic validation + E.164 normalization).

R3 P0 (2026-08-13): wraps Google's libphonenumber (via the `phonenumbers`
Python port) so we get canonical parsing/formatting across regions
instead of ad-hoc per-country regex that will always miss edge cases.

Input: canonical digit string from Layer A (`normalize_spoken_digits`).
Output: `PhoneParseResult` with status + E.164 value.

Status semantics (deliberately libphonenumber-inspired):
  COMPLETE   -> is_valid_number() said yes; value is E.164.
  POSSIBLE   -> is_possible_number() says yes but is_valid says no —
                right shape for the region but not in the actual
                allocated ranges.  Treat as "close, accept for now
                but flag" for booking flows; reject for outbound.
  PARTIAL    -> shorter than any possible number in the region.
                Ask caller for more digits.
  TOO_LONG   -> longer than any possible number.  Caller probably
                glued two numbers together; ask them to restart.
  INVALID    -> libphonenumber rejects it structurally
                (e.g. no country code we can figure out).
  EMPTY      -> no digits at all.

Regions are tenant-configurable (see settings.phone_default_region and
settings.phone_accepted_regions).  We try each accepted region in
order and return the first COMPLETE result; if none is COMPLETE we
return the best POSSIBLE; else the strictest error.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

import phonenumbers
from phonenumbers.phonenumberutil import NumberParseException, ValidationResult

# libphonenumber 8.x returns bare ints from is_possible_number_with_reason
# (the module aliases ValidationResult to the int constants but doesn't wrap
# them in an enum with .name).  Match against the int values directly.
_VR_IS_POSSIBLE = int(ValidationResult.IS_POSSIBLE)
_VR_TOO_SHORT = int(ValidationResult.TOO_SHORT)
_VR_TOO_LONG = int(ValidationResult.TOO_LONG)


class PhoneStatus(str, Enum):
    COMPLETE = "complete"
    POSSIBLE = "possible"
    PARTIAL = "partial"
    TOO_LONG = "too_long"
    INVALID = "invalid"
    EMPTY = "empty"


@dataclass(frozen=True)
class PhoneParseResult:
    status: PhoneStatus
    value: Optional[str] = None      # E.164 on COMPLETE / POSSIBLE
    matched_region: Optional[str] = None
    reason: str = ""
    raw_digits: str = ""             # the canonical string we tried


def parse_phone(
    canonical: str,
    default_region: str = "US",
    accepted_regions: Optional[list[str]] = None,
) -> PhoneParseResult:
    """Parse a canonical digit string against one or more phone regions.

    canonical:        digit string, optionally with leading `+`.  Get
                      this from `normalize_spoken_digits()`.
    default_region:   region to try FIRST (typically the tenant's
                      home country ISO-2 code: "US", "PK", "GB", ...).
    accepted_regions: additional regions to try if default fails.
                      Defaults to just [default_region].

    Returns the best result found across all tried regions.  A
    COMPLETE hit in any region wins immediately.
    """
    if not canonical or canonical.strip("+") == "":
        return PhoneParseResult(status=PhoneStatus.EMPTY, raw_digits=canonical)

    # Normalize 00-prefix international dialing to `+` before handing
    # to libphonenumber (which doesn't understand "00" without a
    # default region even for otherwise-unambiguous numbers).
    if canonical.startswith("00") and not canonical.startswith("+"):
        canonical = "+" + canonical[2:]

    regions_to_try: list[str] = [default_region]
    if accepted_regions:
        for r in accepted_regions:
            if r and r not in regions_to_try:
                regions_to_try.append(r)

    best_result: Optional[PhoneParseResult] = None

    for region in regions_to_try:
        try:
            num = phonenumbers.parse(canonical, region)
        except NumberParseException as e:
            # Structural failure for THIS region; try the next one.
            best_result = _prefer(
                best_result,
                PhoneParseResult(
                    status=PhoneStatus.INVALID,
                    reason=f"[{region}] {e._msg if hasattr(e, '_msg') else str(e)}",
                    raw_digits=canonical,
                ),
            )
            continue

        e164 = phonenumbers.format_number(
            num, phonenumbers.PhoneNumberFormat.E164,
        )

        if phonenumbers.is_valid_number(num):
            # First-wins: return immediately.  For fully-international
            # numbers (starts with `+`), libphonenumber ignores the
            # hint region and derives country from the country_code.
            # Report the ACTUAL region from the parsed country code,
            # not our first-try hint — otherwise a US-tenant probe of
            # "+923335244772" reports matched_region="US" even though
            # the number is PK.
            try:
                actual_region = phonenumbers.region_code_for_number(num)
            except Exception:
                actual_region = None
            return PhoneParseResult(
                status=PhoneStatus.COMPLETE,
                value=e164,
                matched_region=actual_region or region,
                raw_digits=canonical,
            )

        # Not valid — check how far off it is.
        # libphonenumber's IS_POSSIBLE is broader than we want: it
        # includes both "shorter than any valid number for this region"
        # AND "right shape but not in an allocated range."  For voice
        # capture we care about the difference (keep listening vs
        # accept-with-confirmation).  Distinguish by comparing the
        # national significant number length to the region's expected
        # lengths from PhoneNumberMetadata.
        vres = int(phonenumbers.is_possible_number_with_reason(num))
        if vres == _VR_IS_POSSIBLE:
            # Get expected lengths for this region + number type.
            # If national number is shorter than the shortest expected
            # length, that's a PARTIAL (keep listening).  Equal-or-longer
            # but still not "valid" = POSSIBLE (right shape, wrong range).
            nsn_len = len(str(num.national_number))
            try:
                meta = phonenumbers.PhoneMetadata.metadata_for_region(region.upper())
                if meta is not None:
                    # general_desc.possible_length is a tuple of ints
                    possible_lens = list(meta.general_desc.possible_length or ())
                    if possible_lens and nsn_len < min(possible_lens):
                        best_result = _prefer(
                            best_result,
                            PhoneParseResult(
                                status=PhoneStatus.PARTIAL,
                                matched_region=region,
                                reason=f"[{region}] partial: {nsn_len} < min {min(possible_lens)} digits",
                                raw_digits=canonical,
                            ),
                        )
                        continue
            except Exception:
                pass
            best_result = _prefer(
                best_result,
                PhoneParseResult(
                    status=PhoneStatus.POSSIBLE,
                    value=e164,
                    matched_region=region,
                    reason="matches region shape but not in an allocated range",
                    raw_digits=canonical,
                ),
            )
        elif vres == _VR_TOO_SHORT:
            best_result = _prefer(
                best_result,
                PhoneParseResult(
                    status=PhoneStatus.PARTIAL,
                    matched_region=region,
                    reason=f"[{region}] too short",
                    raw_digits=canonical,
                ),
            )
        elif vres == _VR_TOO_LONG:
            best_result = _prefer(
                best_result,
                PhoneParseResult(
                    status=PhoneStatus.TOO_LONG,
                    matched_region=region,
                    reason=f"[{region}] too long",
                    raw_digits=canonical,
                ),
            )
        else:
            best_result = _prefer(
                best_result,
                PhoneParseResult(
                    status=PhoneStatus.INVALID,
                    matched_region=region,
                    reason=f"[{region}] validation_result={vres}",
                    raw_digits=canonical,
                ),
            )

    return best_result or PhoneParseResult(
        status=PhoneStatus.INVALID,
        reason="no region could parse",
        raw_digits=canonical,
    )


# Preference order when combining results across regions:
# COMPLETE > POSSIBLE > PARTIAL > TOO_LONG > INVALID > EMPTY.
_STATUS_RANK = {
    PhoneStatus.COMPLETE: 5,
    PhoneStatus.POSSIBLE: 4,
    PhoneStatus.PARTIAL: 3,
    PhoneStatus.TOO_LONG: 2,
    PhoneStatus.INVALID: 1,
    PhoneStatus.EMPTY: 0,
}


def _prefer(
    current: Optional[PhoneParseResult],
    new: PhoneParseResult,
) -> PhoneParseResult:
    if current is None:
        return new
    return new if _STATUS_RANK[new.status] > _STATUS_RANK[current.status] else current

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class BusinessHours(BaseModel):
    monday: Optional[str] = None
    tuesday: Optional[str] = None
    wednesday: Optional[str] = None
    thursday: Optional[str] = None
    friday: Optional[str] = None
    saturday: Optional[str] = None
    sunday: Optional[str] = None

    def is_open(self, weekday: int, hhmm: str) -> bool:
        days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        window = getattr(self, days[weekday], None)
        if not window:
            return False
        try:
            start, end = window.split("-")
            return start.strip() <= hhmm <= end.strip()
        except ValueError:
            return False


class ServiceOffering(BaseModel):
    name: str
    duration_minutes: int = 30
    description: Optional[str] = None
    price: Optional[str] = None
    # 2026-08-30 (audit Gap 5): container-service duration overrides.
    # For services that are actually a category shared across multiple
    # real bookings (Follow-up visit is the canonical example — post-
    # implant vs post-crown vs post-antibiotic all differ in duration),
    # this map lets a specific answer to the discovery context slot
    # override the base duration_minutes.  Keys are lower-cased,
    # partial-match tokens from the caller's `original_procedure`
    # answer; values are the correct duration in minutes.
    #
    # Example (populated on Follow-up visit in sample-data):
    #   duration_by_original_procedure = {
    #       "implant":        30,   # osseointegration check
    #       "root canal":     45,   # endo recheck / crown seat prep
    #       "crown":          60,   # crown seat visit
    #       "extraction":     15,   # suture removal / dry socket check
    #       "antibiotic":     15,   # short recheck
    #       "filling":        20,   # post-op sensitivity check
    #   }
    #
    # Resolution: `_service_duration(service_name, context=None)` reads
    # the caller's answer, lowercases it, finds the first key whose
    # substring appears in the answer.  Falls back to duration_minutes
    # when no key matches (unknown procedure).
    #
    # Empty dict = no override behavior; regular service.  Non-breaking.
    duration_by_original_procedure: dict[str, int] = Field(
        default_factory=dict,
    )


class BusinessProfile(BaseModel):
    id: str
    name: str
    vertical: str = "clinic"
    timezone: str = "America/New_York"
    hours: BusinessHours = Field(default_factory=BusinessHours)
    services: list[ServiceOffering] = Field(default_factory=list)
    faqs: dict[str, str] = Field(default_factory=dict)
    escalation_phone: Optional[str] = None
    address: Optional[str] = None
    # 2026-08-08 (task #278): explicit business-facts fields so the LLM
    # never has to invent contact info.  The system prompt tells it to
    # ALWAYS use these values verbatim; if unset, say "let me get that
    # for you" and escalate rather than hallucinate a number.
    phone: Optional[str] = None      # Public phone (what callers see)
    email: Optional[str] = None      # Reception email for confirmations
    website: Optional[str] = None
    voice_persona: str = "warm, professional, concise"

    # Legal / compliance greeting toggles.  2026-08-10: switched default
    # to OFF.  With defaults ON the composed greeting was 7-15 sec of
    # µ-law audio — callers were annoyed and hanging up.  Business
    # owners in CA/CO/TX (or anywhere disclosure is required) should
    # explicitly set these True in their profile.
    ai_disclosure_enabled: bool = False
    recording_notice_enabled: bool = False
    # If set, this REPLACES the auto-composed greeting entirely.
    greeting_override: Optional[str] = None

    # 2026-08-29 (BUG-CHR-02): tenant phone-region config for the
    # pre-write phone validator.  Christiaan (+31 Dutch mobile) hit
    # empty completions because clinic_tools defaulted to region="US"
    # only and libphonenumber rejected his 10-digit local Dutch number.
    #
    # Names align with vertical_tools._phone_region_config which was
    # already reading these attributes via getattr — schema just needed
    # to expose them.
    #
    # `phone_default_region` = ISO-3166-alpha-2 code used when the
    # caller provides digits without a country code.  Set to the
    # tenant's local country (US clinic → "US", Ribeira Prime → "PT").
    #
    # `phone_accepted_regions` = additional regions to try if the
    # default fails.  Real-world clinics get calls from expats /
    # tourists / cross-border customers.  Empty list = fall back to
    # the permissive default set in vertical_tools.py.  Tenant can
    # tighten by supplying an explicit narrow list.
    phone_default_region: str = "US"
    phone_accepted_regions: list[str] = Field(default_factory=list)

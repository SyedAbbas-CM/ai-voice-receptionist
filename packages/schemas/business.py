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

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
    voice_persona: str = "warm, professional, concise"

    # Legal / compliance greeting toggles. Default ON — safer to disclose
    # in a state that doesn't require it than skip in one that does.
    # 2026 states requiring some form of AI disclosure: CA, CO, TX and others.
    ai_disclosure_enabled: bool = True
    recording_notice_enabled: bool = True
    # If set, this REPLACES the auto-composed greeting entirely.
    greeting_override: Optional[str] = None

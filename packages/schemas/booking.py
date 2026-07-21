from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class BookingStatus(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    NO_SHOW = "no_show"


class Booking(BaseModel):
    id: Optional[str] = None
    session_id: str
    business_id: str
    caller_name: str
    phone: str
    service: str
    scheduled_for: datetime
    duration_minutes: int = 30
    status: BookingStatus = BookingStatus.PENDING
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)

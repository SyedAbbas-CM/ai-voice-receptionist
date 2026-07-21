"""TCPA compliance layer for outbound AI voice calls.

Under 47 USC § 227(b), an AI-generated voice counts as an "artificial or
prerecorded voice." Placing such a call to a US number without prior
express consent is $500-$1500 in statutory damages PER CALL, class-actionable.

This module makes the compliance path a first-class swap-able adapter:

  - ConsentProvider — abstract "does this number consent to AI calls?"
  - SqliteConsentProvider — local table. Default. Zero deps.
  - HttpConsentProvider — POSTs to a client's consent-service webhook.
  - AlwaysConsentProvider — for internal test numbers ONLY. Never prod.

Plus:
  - is_ai_disclosure_line(text) — verifies the greeting says "this is an
    AI" (or equivalent). Required by 47 CFR § 64.1200 as of 2026.
  - build_disclosure_greeting(business_name) — produces a compliant opener.

Wired into: packages/integrations/dialer_policy.decide_can_call via a
new consent check that runs alongside business_hours + cooldown + DNC.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx


log = logging.getLogger(__name__)


@dataclass
class ConsentRecord:
    phone: str
    consent_granted: bool
    granted_at: Optional[datetime] = None
    source: str = ""
    revoked_at: Optional[datetime] = None

    @property
    def is_current(self) -> bool:
        return self.consent_granted and self.revoked_at is None


class ConsentProvider(ABC):
    name: str = "base"

    @abstractmethod
    async def has_consent(self, phone: str) -> ConsentRecord:
        """Return the consent record for a phone. Missing = not-consented."""

    async def record_consent(self, phone: str, source: str) -> None:
        """Optional: implementations may support recording a new consent
        event (e.g. from a web form). Default is no-op."""
        return None


def _normalize_phone(phone: str) -> str:
    """Normalize to comparable form.

    Rules for US numbers:
      - "+15551234567" -> "+15551234567"
      - "15551234567"  -> "+15551234567"  (add leading + on 11-digit starting with 1)
      - "5551234567"   -> "+15551234567"  (add +1 on 10-digit)
      - "(555) 123-4567" -> "+15551234567"
    Non-US formats (13+ digits, or starting with +) pass through as +digits."""
    if not phone:
        return ""
    phone = phone.strip()
    had_plus = phone.startswith("+")
    digits = "".join(ch for ch in phone if ch.isdigit())
    if not digits:
        return ""
    if had_plus:
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return "+" + digits


class SqliteConsentProvider(ConsentProvider):
    """Local SQLite consent store. Default backend — zero deps beyond stdlib.

    Table schema (auto-created):
        consent_records (
            phone TEXT PRIMARY KEY,
            consent_granted INTEGER,
            granted_at TEXT,
            source TEXT,
            revoked_at TEXT
        )
    """
    name = "sqlite"

    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_table()

    def _init_table(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS consent_records (
                    phone TEXT PRIMARY KEY,
                    consent_granted INTEGER NOT NULL DEFAULT 0,
                    granted_at TEXT,
                    source TEXT,
                    revoked_at TEXT
                )
            """)
            conn.commit()

    async def has_consent(self, phone: str) -> ConsentRecord:
        phone_norm = _normalize_phone(phone)
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT phone, consent_granted, granted_at, source, revoked_at "
                "FROM consent_records WHERE phone = ?",
                (phone_norm,),
            ).fetchone()
        if not row:
            return ConsentRecord(phone=phone_norm, consent_granted=False)
        return ConsentRecord(
            phone=row[0],
            consent_granted=bool(row[1]),
            granted_at=datetime.fromisoformat(row[2]) if row[2] else None,
            source=row[3] or "",
            revoked_at=datetime.fromisoformat(row[4]) if row[4] else None,
        )

    async def record_consent(self, phone: str, source: str) -> None:
        phone_norm = _normalize_phone(phone)
        now = datetime.utcnow().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO consent_records (phone, consent_granted, granted_at, source)
                VALUES (?, 1, ?, ?)
                ON CONFLICT(phone) DO UPDATE SET
                    consent_granted = 1,
                    granted_at = excluded.granted_at,
                    source = excluded.source,
                    revoked_at = NULL
            """, (phone_norm, now, source))
            conn.commit()

    async def revoke(self, phone: str) -> None:
        phone_norm = _normalize_phone(phone)
        now = datetime.utcnow().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE consent_records SET revoked_at = ? WHERE phone = ?",
                (now, phone_norm),
            )
            conn.commit()


class HttpConsentProvider(ConsentProvider):
    """POST to a client's consent-service webhook. Expected response:
    {"consent_granted": true|false, "granted_at": "ISO-8601"|null,
     "source": "web_form"|"call_recording"|...}"""
    name = "http"

    def __init__(self, url: str, headers: Optional[dict] = None, timeout: float = 5.0):
        if not url:
            raise ValueError("HttpConsentProvider needs a URL")
        self.url = url
        self.headers = headers or {}
        self.timeout = timeout

    async def has_consent(self, phone: str) -> ConsentRecord:
        phone_norm = _normalize_phone(phone)
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(self.url, params={"phone": phone_norm}, headers=self.headers)
            if resp.status_code >= 400:
                log.warning("consent lookup HTTP %s for %s", resp.status_code, phone_norm)
                return ConsentRecord(phone=phone_norm, consent_granted=False)
            data = resp.json()
        except Exception as e:
            # Fail CLOSED — no consent lookup, no call. Never fail open on TCPA.
            log.warning("consent lookup failed: %s", e)
            return ConsentRecord(phone=phone_norm, consent_granted=False)
        return ConsentRecord(
            phone=phone_norm,
            consent_granted=bool(data.get("consent_granted")),
            granted_at=(datetime.fromisoformat(data["granted_at"])
                        if data.get("granted_at") else None),
            source=data.get("source", ""),
        )


class AlwaysConsentProvider(ConsentProvider):
    """Test-only. Assumes every number has consented. Never use in prod."""
    name = "always"

    async def has_consent(self, phone: str) -> ConsentRecord:
        return ConsentRecord(
            phone=_normalize_phone(phone),
            consent_granted=True,
            granted_at=datetime.utcnow(),
            source="test_provider",
        )


def build_consent_provider(kind: str, **kwargs) -> ConsentProvider:
    kind = (kind or "sqlite").lower()
    if kind == "sqlite":
        return SqliteConsentProvider(**kwargs)
    if kind == "http":
        return HttpConsentProvider(**kwargs)
    if kind == "always":
        return AlwaysConsentProvider()
    raise ValueError(f"unknown consent provider: {kind!r}")


# ------------------------------------------------------------------
# AI disclosure — required by FCC 47 CFR § 64.1200 as amended 2024
# ------------------------------------------------------------------

_DISCLOSURE_PHRASES = [
    re.compile(r"\bthis is (?:an? )?(?:AI|automated|artificial|virtual)\b", re.I),
    re.compile(r"\b(?:AI|automated) (?:voice )?(?:assistant|agent|receptionist)\b", re.I),
    re.compile(r"\bnot (?:a )?human\b", re.I),
]


def is_ai_disclosure_line(text: str) -> bool:
    """True if the text contains a clear AI disclosure. Used to validate
    greetings before we dial."""
    if not text:
        return False
    return any(p.search(text) for p in _DISCLOSURE_PHRASES)


def build_disclosure_greeting(business_name: str, caller_name: Optional[str] = None) -> str:
    """Produce a compliant opening line. Sample:
        "Hi Bob, this is an AI assistant calling from SubtoDealz about
         your property listing. Is now a good time?"
    """
    who = f"Hi {caller_name}, " if caller_name else "Hi, "
    return (
        f"{who}this is an AI assistant calling on behalf of {business_name}. "
        f"Is now a good time to talk?"
    )

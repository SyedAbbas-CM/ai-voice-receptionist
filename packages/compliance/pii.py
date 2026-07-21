"""PII redaction — swap-in-place adapter.

Every transcript we store in SQLite (or ship to a CRM / dashboard) can
contain phone numbers, credit-card digits, SSN, DOB, email addresses.
For HIPAA/PCI-adjacent clients we can't store those in plaintext.

This module provides:
  - PIIRedactor — abstract interface (redact_text / redact_dict)
  - RegexPIIRedactor — local, zero-dep. Fast. Handles the ~90% of English
    PII you'll actually see: phones, cards, SSN, email, DOB.
  - PresidioPIIRedactor — optional Microsoft Presidio integration for
    NER-based detection (names, addresses). Requires `pip install presidio-analyzer`.
  - NoopPIIRedactor — off. Default.

Redactions are IRREVERSIBLE — we replace matches with tokens like
[PHONE], [CARD], [SSN]. If you need reversible tokenization (e.g. show
the caller their own phone back in a confirmation), build a separate
tokenizer layer on top; don't try to make this one two-way.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RedactionResult:
    text: str
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total_redactions(self) -> int:
        return sum(self.counts.values())

    @property
    def had_pii(self) -> bool:
        return self.total_redactions > 0


class PIIRedactor(ABC):
    name: str = "base"

    @abstractmethod
    def redact_text(self, text: str) -> RedactionResult:
        ...

    def redact_dict(self, data: dict, fields: Optional[list[str]] = None) -> tuple[dict, dict[str, int]]:
        """Recursively redact string values in a dict. If `fields` is given,
        only touch those keys; otherwise touch every string."""
        counts: dict[str, int] = {}
        out = {}
        for k, v in (data or {}).items():
            if fields is not None and k not in fields:
                out[k] = v
                continue
            if isinstance(v, str):
                r = self.redact_text(v)
                out[k] = r.text
                for kind, n in r.counts.items():
                    counts[kind] = counts.get(kind, 0) + n
            elif isinstance(v, dict):
                sub, sub_counts = self.redact_dict(v, fields)
                out[k] = sub
                for kind, n in sub_counts.items():
                    counts[kind] = counts.get(kind, 0) + n
            else:
                out[k] = v
        return out, counts


class NoopPIIRedactor(PIIRedactor):
    """PII redaction OFF. Use only in dev / testing."""
    name = "noop"

    def redact_text(self, text: str) -> RedactionResult:
        return RedactionResult(text=text or "", counts={})


class RegexPIIRedactor(PIIRedactor):
    """Regex-only PII redactor. Covers the common English PII types.

    Ordering matters: card/SSN are matched BEFORE phone because a 16-digit
    number could match both. Card wins."""
    name = "regex"

    # Order = precedence. First match wins per span.
    _PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
        # Credit cards: 13-19 digits, with optional separators
        ("CARD", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
        # SSN: 3-2-4 with dashes/spaces/pure digits
        ("SSN", re.compile(r"\b\d{3}[-\s]?\d{2}[-\s]?\d{4}\b")),
        # Phone: NANP formats + international +
        ("PHONE", re.compile(
            r"(?:(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4})"
        )),
        # Email
        ("EMAIL", re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")),
        # Date of birth (MM/DD/YYYY, YYYY-MM-DD)
        ("DOB", re.compile(r"\b(?:19|20)\d{2}[-/](?:0?[1-9]|1[0-2])[-/](?:0?[1-9]|[12]\d|3[01])\b")),
        ("DOB", re.compile(r"\b(?:0?[1-9]|1[0-2])[-/](?:0?[1-9]|[12]\d|3[01])[-/](?:19|20)\d{2}\b")),
    ]

    def redact_text(self, text: str) -> RedactionResult:
        if not text:
            return RedactionResult(text="", counts={})
        counts: dict[str, int] = {}
        out = text
        for label, pattern in self._PATTERNS:
            def _sub(m):
                counts[label] = counts.get(label, 0) + 1
                return f"[{label}]"
            out = pattern.sub(_sub, out)
        return RedactionResult(text=out, counts=counts)


class PresidioPIIRedactor(PIIRedactor):
    """NER-based redactor via Microsoft Presidio. Catches names + locations
    the regex layer can't. Lazy-import so we don't force the dep."""
    name = "presidio"

    def __init__(self, entities: Optional[list[str]] = None):
        self.entities = entities or [
            "PERSON", "PHONE_NUMBER", "EMAIL_ADDRESS", "CREDIT_CARD",
            "US_SSN", "LOCATION", "DATE_TIME",
        ]
        self._analyzer = None
        self._anonymizer = None

    def _load(self):
        if self._analyzer is not None:
            return
        try:
            from presidio_analyzer import AnalyzerEngine
            from presidio_anonymizer import AnonymizerEngine
        except ImportError as e:
            raise RuntimeError(
                "Presidio not installed. `pip install presidio-analyzer presidio-anonymizer` "
                "or fall back to RegexPIIRedactor via build_pii_redactor(kind='regex')."
            ) from e
        self._analyzer = AnalyzerEngine()
        self._anonymizer = AnonymizerEngine()

    def redact_text(self, text: str) -> RedactionResult:
        if not text:
            return RedactionResult(text="", counts={})
        self._load()
        results = self._analyzer.analyze(text=text, entities=self.entities, language="en")
        counts: dict[str, int] = {}
        for r in results:
            counts[r.entity_type] = counts.get(r.entity_type, 0) + 1
        anonymized = self._anonymizer.anonymize(text=text, analyzer_results=results)
        return RedactionResult(text=anonymized.text, counts=counts)


def build_pii_redactor(kind: str = "regex", **kwargs) -> PIIRedactor:
    """Factory: 'noop' | 'regex' | 'presidio'.

    Called from session_manager at transcript-write time. Env-swappable
    via settings.pii_redactor."""
    kind = (kind or "noop").lower()
    if kind == "noop":
        return NoopPIIRedactor()
    if kind == "regex":
        return RegexPIIRedactor()
    if kind == "presidio":
        return PresidioPIIRedactor(**kwargs)
    raise ValueError(f"unknown PII redactor kind: {kind!r}")

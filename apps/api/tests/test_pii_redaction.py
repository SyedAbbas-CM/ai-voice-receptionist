"""PII redaction tests. Regex must catch the common English cases.
No live Presidio here — that's an optional dep."""
from __future__ import annotations

import pytest

from packages.compliance import (
    NoopPIIRedactor,
    RegexPIIRedactor,
    build_pii_redactor,
)


def test_noop_passes_through():
    r = NoopPIIRedactor()
    result = r.redact_text("My phone is 555-123-4567.")
    assert result.text == "My phone is 555-123-4567."
    assert result.had_pii is False


def test_regex_redacts_phone():
    r = RegexPIIRedactor()
    result = r.redact_text("Call me at 555-123-4567.")
    assert "555-123-4567" not in result.text
    assert "[PHONE]" in result.text
    assert result.counts.get("PHONE") == 1


def test_regex_redacts_phone_no_dashes():
    r = RegexPIIRedactor()
    result = r.redact_text("Phone is 5551234567")
    assert "5551234567" not in result.text
    assert "[PHONE]" in result.text


def test_regex_redacts_phone_with_country_code():
    r = RegexPIIRedactor()
    result = r.redact_text("Reach me at +1 (555) 123-4567")
    assert "555" not in result.text or "[PHONE]" in result.text


def test_regex_redacts_credit_card():
    r = RegexPIIRedactor()
    result = r.redact_text("My card is 4111 1111 1111 1111.")
    assert "4111" not in result.text
    assert "[CARD]" in result.text


def test_regex_redacts_ssn():
    r = RegexPIIRedactor()
    result = r.redact_text("SSN 123-45-6789")
    assert "123-45-6789" not in result.text
    assert "[SSN]" in result.text


def test_regex_redacts_email():
    r = RegexPIIRedactor()
    result = r.redact_text("Email me at john@example.com")
    assert "john@example.com" not in result.text
    assert "[EMAIL]" in result.text


def test_regex_redacts_dob():
    r = RegexPIIRedactor()
    result = r.redact_text("DOB is 1985-06-15 and also 06/15/1985.")
    assert "1985-06-15" not in result.text
    assert "06/15/1985" not in result.text
    assert result.counts.get("DOB", 0) >= 2


def test_regex_handles_multi_pii_in_one_line():
    r = RegexPIIRedactor()
    result = r.redact_text(
        "I'm John, my number is 555-123-4567 and my email is j@x.com."
    )
    assert result.counts.get("PHONE") == 1
    assert result.counts.get("EMAIL") == 1
    assert result.total_redactions >= 2
    assert result.had_pii is True


def test_regex_leaves_clean_text_untouched():
    r = RegexPIIRedactor()
    result = r.redact_text("Just here to book an appointment please.")
    assert result.text == "Just here to book an appointment please."
    assert result.had_pii is False


def test_redact_dict_only_touches_string_values():
    r = RegexPIIRedactor()
    data = {
        "name": "John Carter",  # not in the regex set — passes through
        "phone": "555-123-4567",
        "notes": "Call at 555-000-1111",
        "count": 42,             # not a string — untouched
        "nested": {"email": "x@y.com"},
    }
    out, counts = r.redact_dict(data)
    assert out["name"] == "John Carter"
    assert "[PHONE]" in out["phone"]
    assert "[PHONE]" in out["notes"]
    assert out["count"] == 42
    assert "[EMAIL]" in out["nested"]["email"]
    assert counts.get("PHONE") == 2
    assert counts.get("EMAIL") == 1


def test_redact_dict_respects_field_allowlist():
    r = RegexPIIRedactor()
    data = {"leak_this": "555-123-4567", "keep_this": "555-999-1111"}
    out, _ = r.redact_dict(data, fields=["leak_this"])
    assert "[PHONE]" in out["leak_this"]
    assert out["keep_this"] == "555-999-1111"


def test_factory_returns_correct_kind():
    assert build_pii_redactor("noop").name == "noop"
    assert build_pii_redactor("regex").name == "regex"
    with pytest.raises(ValueError):
        build_pii_redactor("magic")

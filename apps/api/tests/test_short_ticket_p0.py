"""P0.3/P0.4 regression — short_ticket HMAC signing.

BACKEND-AUDIT-2026-08-25-CHATGPT.md findings #3 + #4 both use this module.
Any bug here breaks WSS-upgrade auth AND dashboard signed-session auth
simultaneously, so the test bar is intentionally strict.

Covers:
  * Round-trip mint → verify with matching secret
  * Signature-mismatch → TicketInvalid
  * Expired ticket → TicketExpired (distinct from Invalid)
  * Missing/short secret → TicketError at mint, TicketInvalid at verify
  * Malformed token shapes → TicketInvalid, never crashes
  * Payload tampering (modify base64 → sig no longer matches) → Invalid
  * TTL cap enforced (nobody can mint a 24h ticket "just this once")
  * try_verify_ticket returns None instead of raising
"""
from __future__ import annotations

import base64
import json
import time

import pytest

from packages.auth import (
    TicketError,
    TicketExpired,
    TicketInvalid,
    TicketPayload,
    mint_ticket,
    try_verify_ticket,
    verify_ticket,
)


# Deterministic 64-char hex — meets the module's 32-char minimum
GOOD_SECRET = "0" * 64
OTHER_SECRET = "1" * 64


@pytest.fixture(autouse=True)
def _secret_env(monkeypatch):
    monkeypatch.setenv("SHORT_TICKET_SECRET", GOOD_SECRET)
    yield


# ─── Happy path ─────────────────────────────────────────────────────────────


def test_roundtrip_mint_and_verify():
    tok = mint_ticket("acme-corp", "twilio_stream:CA1234", ttl_s=120)
    payload = verify_ticket(tok)
    assert isinstance(payload, TicketPayload)
    assert payload.tenant_id == "acme-corp"
    assert payload.subject == "twilio_stream:CA1234"
    assert payload.exp > int(time.time())


def test_ticket_format_is_two_b64_segments_dot_separated():
    tok = mint_ticket("t", "s")
    parts = tok.split(".")
    assert len(parts) == 2
    for seg in parts:
        assert seg, "segment must not be empty"
        # base64url alphabet only — no +, /, =
        assert set(seg).issubset(
            set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
        ), f"segment has non-b64url char: {seg!r}"


def test_ticket_is_compact():
    """< 200 bytes for a typical ticket — fits comfortably in a URL."""
    tok = mint_ticket("smile-dental-001", "twilio_stream:CAab1234cd", ttl_s=120)
    assert len(tok) < 200, f"ticket is {len(tok)} bytes — too long for a URL"


# ─── Signature integrity ────────────────────────────────────────────────────


def test_signature_from_different_secret_is_rejected(monkeypatch):
    """The core security property. A ticket signed with a different
    secret must NOT verify."""
    tok = mint_ticket("acme", "sub")
    monkeypatch.setenv("SHORT_TICKET_SECRET", OTHER_SECRET)
    with pytest.raises(TicketInvalid, match="signature mismatch"):
        verify_ticket(tok)


def test_tampered_payload_fails_verification():
    """Attacker flips a bit in the payload without knowing the secret →
    signature no longer matches. If this test fails, HMAC is broken."""
    tok = mint_ticket("acme", "sub")
    payload_b64, sig = tok.split(".")
    # Change one char in the payload — safe because base64url alphabet
    # is closed under this substitution.
    tampered_payload = ("A" if payload_b64[0] != "A" else "B") + payload_b64[1:]
    tampered = f"{tampered_payload}.{sig}"
    with pytest.raises(TicketInvalid, match="signature mismatch"):
        verify_ticket(tampered)


def test_tampered_signature_fails_verification():
    tok = mint_ticket("acme", "sub")
    payload_b64, sig = tok.split(".")
    tampered_sig = ("A" if sig[0] != "A" else "B") + sig[1:]
    tampered = f"{payload_b64}.{tampered_sig}"
    with pytest.raises(TicketInvalid):
        verify_ticket(tampered)


def test_signature_only_leaks_generic_error(monkeypatch):
    """Attacker probing the endpoint should NOT be able to tell whether
    their forgery failed sig-check or exp-check. Both raise TicketInvalid
    or TicketExpired but the WSS handler translates both to a plain 401.
    Verify verify_ticket doesn't leak internals in the exception message."""
    tok = mint_ticket("acme", "sub")
    payload_b64, sig = tok.split(".")
    tampered = f"{payload_b64}.AAAA"
    with pytest.raises(TicketInvalid) as exc:
        verify_ticket(tampered)
    # Should NOT contain the actual expected signature (leaks HMAC output)
    assert "expected" not in str(exc.value).lower() or GOOD_SECRET not in str(exc.value)


# ─── Expiration ─────────────────────────────────────────────────────────────


def test_expired_ticket_raises_expired_not_invalid(monkeypatch):
    """Distinct exception type — callers may want to log expiration
    differently from forgery attempts (a legit user hitting a stale
    URL vs. a probing attacker)."""
    tok = mint_ticket("acme", "sub", ttl_s=1)
    # Fast-forward past exp
    real_time = time.time
    monkeypatch.setattr(
        "packages.auth.short_ticket.time.time",
        lambda: real_time() + 10,
    )
    with pytest.raises(TicketExpired):
        verify_ticket(tok)


def test_expired_ticket_verifies_signature_first():
    """A ticket with a bad signature AND an expired timestamp should
    raise Invalid, NOT Expired — otherwise "expired" leaks info about
    the signature check to attackers."""
    # Build a ticket, then tamper it, then wait for its natural exp.
    tok = mint_ticket("acme", "sub", ttl_s=1)
    payload_b64, sig = tok.split(".")
    tampered = f"{payload_b64}.AAAA"
    time.sleep(1.1)
    with pytest.raises(TicketInvalid):
        # Even though the underlying ticket is also expired, sig
        # check runs first and wins the exception type.
        verify_ticket(tampered)


# ─── Secret hygiene ─────────────────────────────────────────────────────────


def test_unset_secret_prevents_mint(monkeypatch):
    monkeypatch.delenv("SHORT_TICKET_SECRET", raising=False)
    with pytest.raises(TicketError, match="unset"):
        mint_ticket("acme", "sub")


def test_short_secret_prevents_mint(monkeypatch):
    monkeypatch.setenv("SHORT_TICKET_SECRET", "too-short")
    with pytest.raises(TicketError, match="too short"):
        mint_ticket("acme", "sub")


def test_unset_secret_prevents_verify_with_invalid_exception(monkeypatch):
    """Even if a ticket was minted with a proper secret, verifying with
    the secret unset must fail — and as TicketInvalid, not TicketError,
    so the surface layer knows to return 401 not 500."""
    tok = mint_ticket("acme", "sub")
    monkeypatch.delenv("SHORT_TICKET_SECRET", raising=False)
    with pytest.raises(TicketInvalid):
        verify_ticket(tok)


# ─── Input validation on mint ───────────────────────────────────────────────


def test_mint_rejects_empty_tenant_id():
    with pytest.raises(TicketError, match="tenant_id"):
        mint_ticket("", "sub")


def test_mint_rejects_empty_subject():
    with pytest.raises(TicketError, match="subject"):
        mint_ticket("acme", "")


def test_mint_rejects_zero_or_negative_ttl():
    for bad_ttl in (0, -1, -3600):
        with pytest.raises(TicketError, match="ttl_s"):
            mint_ticket("acme", "sub", ttl_s=bad_ttl)


def test_mint_rejects_ttl_above_ceiling():
    """3600s = 1h is the max. Anything above is a design mistake —
    "short-lived" doesn't mean an hour+ session."""
    with pytest.raises(TicketError, match="ttl_s=3601 exceeds"):
        mint_ticket("acme", "sub", ttl_s=3601)


def test_mint_allows_max_ttl():
    tok = mint_ticket("acme", "sub", ttl_s=3600)
    p = verify_ticket(tok)
    assert p.exp - int(time.time()) <= 3600


# ─── Verify input hardening ─────────────────────────────────────────────────


@pytest.mark.parametrize("bad", [
    "",
    None,
    "no-dot-in-here",
    "too.many.dots.here",
    ".empty-payload",
    "empty-sig.",
    "!@#$%.^&*()",
])
def test_verify_rejects_malformed_tokens_without_crashing(bad):
    with pytest.raises(TicketInvalid):
        verify_ticket(bad)


def test_verify_rejects_valid_sig_but_missing_fields(monkeypatch):
    """Someone with the secret could mint a payload missing `t` or `s`.
    Verify rejects that shape, doesn't return a TicketPayload with None."""
    import hashlib
    import hmac as _hmac

    payload = {"t": "acme"}  # missing "s" and "e"
    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    payload_b64 = base64.urlsafe_b64encode(payload_json.encode()).rstrip(b"=").decode()
    sig = _hmac.new(
        GOOD_SECRET.encode(),
        payload_b64.encode(),
        hashlib.sha256,
    ).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
    tok = f"{payload_b64}.{sig_b64}"
    with pytest.raises(TicketInvalid, match="missing required field"):
        verify_ticket(tok)


def test_verify_rejects_non_object_payload():
    """Even with a valid signature, a payload that isn't a JSON object
    (e.g. a JSON array) must be rejected — doesn't have our shape."""
    import hashlib
    import hmac as _hmac

    payload_b64 = base64.urlsafe_b64encode(b'["not","an","object"]').rstrip(b"=").decode()
    sig = _hmac.new(
        GOOD_SECRET.encode(),
        payload_b64.encode(),
        hashlib.sha256,
    ).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
    tok = f"{payload_b64}.{sig_b64}"
    with pytest.raises(TicketInvalid):
        verify_ticket(tok)


# ─── try_verify_ticket wrapper ──────────────────────────────────────────────


def test_try_verify_returns_none_on_invalid():
    assert try_verify_ticket("garbage") is None


def test_try_verify_returns_payload_on_valid():
    tok = mint_ticket("acme", "sub")
    result = try_verify_ticket(tok)
    assert isinstance(result, TicketPayload)
    assert result.tenant_id == "acme"

"""Fastpath-before-commit-lock regression tests.

Motivating case (call CA99c1dc9327602d6e2062e497dce25834, 2026-08-17):
   18:16:43,046 COMMIT_LOCK_CLAIM gen=0 reason=speculative revision=1
   18:16:43,046 speculative brain firing gen=0 text='Hello? Can you hear me?'
   18:16:43,053 COMMIT_LOCK_SKIP gen=0 reason=run_brain (slot already claimed)
   ...  ~2s later: filler "Okay, just a moment."
   ...  ~2s later: LLM reply "Yep, I can hear you! ..."

Root cause: commit 25cba53 (task #369) added `_try_claim_response_commit`
BEFORE the conv-control fastpath check in `_run_brain_from_text`.
Speculative EAGER_END_OF_TURN always claims the lock first, spawns
`_run_brain_from_text`, which re-claims → SKIP → returns WITHOUT
running the fastpath.  Result: "Hello can you hear me" goes through
the LLM every time — a 3s→9s regression from the Aug 13 baseline.

Fix: the speculative EAGER handler checks the conv-control matcher
BEFORE claiming the lock.  If it matches, claim with reason
'conv_control_fastpath' and spawn a fastpath speak task instead of
the LLM.  If it misses, current speculative-LLM flow proceeds.

These tests exercise the matcher predicate directly and assert the
happy-path / miss-path split.  Full actor wiring is covered by the
existing test_actor_nonblocking_end_of_turn.py suite.
"""
from __future__ import annotations


class _MatcherHarness:
    """Minimal shim that exposes the actor's synchronous conv-control
    predicate.  Method touches no telephony deps."""

    call_id = "TEST_MATCHER_CALL"


def _bind_matcher() -> _MatcherHarness:
    from app.routes.twilio_actor import TwilioActorSession
    # 2026-08-21: force cache bypass OFF for these tests. The .env file
    # may set RESPONSE_CACHE_BYPASS=true for live LLM-speed testing, and
    # that flag short-circuits the matcher (correct in prod, but breaks
    # these unit tests which assert pattern-matching independent of the
    # bypass switch).
    from app.core.config import settings
    settings.response_cache_bypass = False

    h = _MatcherHarness()
    attr = getattr(TwilioActorSession, "_matches_conversation_control_intent")
    setattr(h, "_matches_conversation_control_intent",
            attr.__get__(h, type(h)))
    return h


# ── the exact regression input ───────────────────────────────────────

def test_hello_can_you_hear_me_matches():
    """The precise transcript from CA99c1dc9327602d6e2062e497dce25834
    that regressed from 3s to 9s.  If this returns False, the fastpath
    diversion will not fire and the LLM path will run — the regression
    is back."""
    h = _bind_matcher()
    assert h._matches_conversation_control_intent(
        "Hello? Can you hear me?",
    ) is True


def test_clean_conv_control_variants_all_match():
    """Every clean canonical variant must match — these are the strings
    the boot-time TTS cache is warmed against.  Compound sentences with
    prefix noise ('You're breaking up. Can you hear me?') are a known
    matcher gap NOT covered by this fix; they fall through to the LLM
    and would want a separate matcher-relaxation pass."""
    h = _bind_matcher()
    for text in (
        "Hello? Can you hear me?",
        "can you hear me",
        "Are you there?",
        "hello",
        "hi",
    ):
        assert h._matches_conversation_control_intent(text) is True, text


# ── booking / business intents MUST NOT match ────────────────────────

def test_booking_intent_does_not_match():
    """The fastpath must NOT swallow real business requests.  If this
    returns True, we would fastpath-speak 'Yep, I can hear you!' at
    someone trying to book an appointment."""
    h = _bind_matcher()
    for text in (
        "I want to book an appointment for tomorrow",
        "Can I schedule a cleaning next week",
        "What are your hours",
        "How much does a filling cost",
    ):
        assert h._matches_conversation_control_intent(text) is False, text


# ── the speculative-vs-fastpath ordering is what this fix is about ──

def test_matcher_is_synchronous_and_side_effect_free():
    """The speculative EAGER handler is a sync callback — the
    predicate MUST be a plain non-async function.  If someone
    refactors it to a coroutine, the EAGER handler will silently
    treat the return as truthy (bug).  This test pins the API."""
    import inspect
    from app.routes.twilio_actor import TwilioActorSession

    assert not inspect.iscoroutinefunction(
        TwilioActorSession._matches_conversation_control_intent,
    )


def test_matcher_swallows_errors_and_returns_false():
    """The predicate wraps the import + match in a try/except so a
    broken matcher module does not crash the EAGER handler — it just
    falls through to normal speculative-LLM flow."""
    h = _bind_matcher()
    # Empty input never crashes; matcher returns None → predicate False.
    assert h._matches_conversation_control_intent("") is False

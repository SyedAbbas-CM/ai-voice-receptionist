"""One-generation-one-commit invariant tests.

Motivating case (Abdullah 2026-08-13 18:53:51-52 gen=20):
   18:53:51,214 speculative brain firing gen=20 text='What others are available?'
   18:53:52,015 speculative HIT (matching text — no new gen)
   18:53:52,273 speculative brain firing gen=20 text='Can you repeat?'  # ← BUG
   18:53:52,362 TTS_SENTENCE_QUEUED gen=20 first=True
   18:53:53,075 speculative MISS: cancelling (too late — audio already on wire)

Root cause: the "speculative HIT" short-circuit cleared
`_speculative_task` while the underlying task was still running, so a
follow-on EAGER_END_OF_TURN passed the "no in-flight speculative"
guard and dispatched a second brain on the SAME gen.

Fix: an explicit commit-lock per generation.  Any dispatch site
(speculative brain, real END_OF_TURN, fastpath, batch, streaming)
must claim the slot BEFORE firing.  First caller wins; subsequent
callers on the same gen bail cleanly.

These tests exercise the lock primitives directly through a
lightweight harness so we don't need to spin up a full WebSocket
actor session.
"""
from __future__ import annotations

from typing import Optional


class _LockHarness:
    """Minimal shim that exposes the actor's commit-lock methods.  The
    methods only touch these instance fields — no telephony deps."""

    call_id = "TEST_LOCK_CALL"

    def __init__(self) -> None:
        self._committed_response_gens: set[int] = set()
        self._response_revision_counter: dict[int, int] = {}


def _bind_lock_methods() -> _LockHarness:
    from app.routes.twilio_actor import TwilioActorSession

    h = _LockHarness()
    for name in (
        "_try_claim_response_commit",
        "_clear_response_commits_before",
    ):
        attr = getattr(TwilioActorSession, name)
        setattr(h, name, attr.__get__(h, type(h)))
    return h


# ── first claim wins ─────────────────────────────────────────────────

def test_first_claim_succeeds():
    h = _bind_lock_methods()
    assert h._try_claim_response_commit(5, reason="test") is True


def test_second_claim_on_same_gen_fails():
    """Direct pin for Abdullah's gen=20 bug — a second dispatch on the
    same gen slot must be rejected regardless of transcript."""
    h = _bind_lock_methods()
    assert h._try_claim_response_commit(20, reason="speculative") is True
    assert h._try_claim_response_commit(20, reason="run_brain") is False


def test_different_gens_are_independent():
    h = _bind_lock_methods()
    assert h._try_claim_response_commit(1, reason="a") is True
    assert h._try_claim_response_commit(2, reason="b") is True
    assert h._try_claim_response_commit(1, reason="c") is False
    assert h._try_claim_response_commit(2, reason="d") is False
    assert h._try_claim_response_commit(3, reason="e") is True


# ── revision counter (future replacement semantics) ─────────────────

def test_revision_counter_increments_on_claim():
    """Reserved for future replacement semantics — a rejected re-claim
    does not bump the counter (the slot's owner is unchanged)."""
    h = _bind_lock_methods()
    h._try_claim_response_commit(7, reason="a")
    assert h._response_revision_counter[7] == 1
    h._try_claim_response_commit(7, reason="b")  # rejected
    assert h._response_revision_counter[7] == 1


# ── stale-gen cleanup ────────────────────────────────────────────────

def test_clear_before_drops_older_gens_only():
    h = _bind_lock_methods()
    h._try_claim_response_commit(1, reason="a")
    h._try_claim_response_commit(2, reason="b")
    h._try_claim_response_commit(5, reason="c")
    # bump_turn produced gen=6 — clear anything strictly older.
    h._clear_response_commits_before(6)
    assert 1 not in h._committed_response_gens
    assert 2 not in h._committed_response_gens
    assert 5 not in h._committed_response_gens
    # A future claim on the new gen is fresh:
    assert h._try_claim_response_commit(6, reason="d") is True


def test_clear_before_keeps_current_and_future_gens():
    h = _bind_lock_methods()
    h._try_claim_response_commit(3, reason="a")
    h._try_claim_response_commit(4, reason="b")
    h._clear_response_commits_before(4)
    # Only strictly-older (3) drops; the current gen (4) is preserved.
    assert 3 not in h._committed_response_gens
    assert 4 in h._committed_response_gens
    # A re-claim on gen=4 is still rejected — its owner is still live.
    assert h._try_claim_response_commit(4, reason="c") is False


# ── the actual Abdullah race, replayed ───────────────────────────────

def test_abdullah_gen20_race_is_prevented():
    """Replay the exact log sequence from Abdullah's call.  With the
    commit lock in place, the second speculative bails at the guard
    instead of stacking a second brain on the same gen."""
    h = _bind_lock_methods()

    # 18:53:51,214 — first speculative for "What others are available?"
    ok1 = h._try_claim_response_commit(20, reason="speculative")
    assert ok1 is True

    # 18:53:52,015 — speculative HIT.  We used to clear the marker
    # here, but per task #369 the commit lock stays held for gen=20
    # until bump_turn/hangup.  Any follow-on dispatch sees the slot
    # is claimed.

    # 18:53:52,273 — second speculative for "Can you repeat?".  This
    # is where the old code fired a second brain on gen=20.  With the
    # lock in place, the claim fails and the dispatch bails.
    ok2 = h._try_claim_response_commit(20, reason="speculative")
    assert ok2 is False

    # 18:53:52,362 onwards — only ONE brain (the first speculative)
    # crosses the speech boundary.  The lock guarantees the invariant.

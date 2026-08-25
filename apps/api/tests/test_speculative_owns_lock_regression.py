"""T4 regression tests — speculative brain must not self-veto its own lock.

Motivating case (2026-08-19, US caller CA813939...):
   speculative claim gen=N reason=speculative → OK
   spawn _run_brain_from_text
   _run_brain_from_text re-claims reason=run_brain → SKIP (own lock)
   → fastpath + response_cache + streaming prelude never runs
   → falls through to _brain_job (batch, no cache/streaming)
   → filler at 1.5s
   → real reply at 3-5s

Fix (T4, 2026-08-19): `_run_brain_from_text` now accepts an
`owns_lock=False` keyword parameter.  The speculative dispatcher
passes `owns_lock=True` so the spawned brain skips the redundant
re-claim.  The lock semantics (task #369, Abdullah's gen=20 race)
remain intact: the outer speculative dispatcher STILL claims BEFORE
spawning; the guard against double-brain-on-same-gen still fires
against any OTHER caller that tries to claim.

These tests pin the API shape so a future refactor can't quietly
break the ownership pass-through.
"""
from __future__ import annotations

import inspect


def test_run_brain_from_text_accepts_owns_lock_kwarg():
    """The keyword MUST be present or the speculative dispatcher's
    call will TypeError at runtime and every non-fastpath turn will
    regress to the slow path."""
    from app.routes.twilio_actor import TwilioActorSession

    sig = inspect.signature(TwilioActorSession._run_brain_from_text)
    assert "owns_lock" in sig.parameters, (
        "_run_brain_from_text must accept owns_lock — the speculative "
        "dispatcher relies on it to skip the redundant lock re-claim."
    )
    p = sig.parameters["owns_lock"]
    # Must be keyword-only so positional-call rot can't silently pass a
    # transcript through as owns_lock.
    assert p.kind == inspect.Parameter.KEYWORD_ONLY, (
        f"owns_lock must be keyword-only, got {p.kind}"
    )
    # Default False so direct callers (non-speculative paths) keep the
    # old lock-claim behavior.
    assert p.default is False, (
        f"owns_lock must default to False, got {p.default!r}"
    )


def test_speculative_dispatcher_passes_owns_lock_true():
    """Grep-based check: the file must call
    _run_brain_from_text(..., owns_lock=True) at least once — that's
    the ONLY place the speculative dispatcher lives.  If a refactor
    drops the kwarg, this fails loudly."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "app" / "routes" / "twilio_actor.py"
    text = src.read_text()
    assert "owns_lock=True" in text, (
        "twilio_actor.py must pass owns_lock=True to _run_brain_from_text "
        "from the speculative dispatcher.  Missing kwarg means the "
        "speculative brain self-vetos on every turn (T4 regression)."
    )
    # And the log line must include owns_lock so live-call debugging
    # can see whether ownership was passed correctly.
    assert 'owns_lock=%s' in text, (
        "stream-brain log line must include owns_lock=%s so debug traces "
        "can distinguish owned-lock speculative dispatches from direct calls."
    )


def test_non_speculative_call_sites_still_default_to_owns_lock_false():
    """The legacy inline path (line ~3613) and the interrupt path
    (line ~4352) call _run_brain_from_text WITHOUT the kwarg — they
    must keep the historical behavior of claiming the lock themselves.
    Grep for those exact call shapes."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "app" / "routes" / "twilio_actor.py"
    text = src.read_text()
    # These two shapes must exist untouched (no owns_lock kwarg):
    #   self._run_brain_from_text(text, turn_gen)          <- legacy inline
    #   self._run_brain_from_text(text, turn_gen)          <- interruption
    # If someone slaps owns_lock=True on those, the lock gets bypassed
    # for a caller that never claimed — Abdullah's gen=20 race can
    # reappear.
    assert "self._run_brain_from_text(text, turn_gen),\n" in text or \
           "self._run_brain_from_text(\n                text, turn_gen,\n            )" in text, (
        "Legacy/interruption call sites for _run_brain_from_text must "
        "still exist without owns_lock=True — they claim the lock inside "
        "the callee.  Bypassing that reintroduces double-brain-on-same-gen."
    )

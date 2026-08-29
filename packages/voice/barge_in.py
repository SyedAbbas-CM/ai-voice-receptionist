"""Barge-in / interruption handling.

When the caller starts talking while the agent is speaking, we have two
choices:
  - real interruption ("stop", "wait", "actually let me change that") →
    flush TTS output, cancel in-flight LLM, commit the caller's turn.
  - backchannel ("mhm", "yeah", "uh-huh") → keep talking; the caller is
    just acknowledging, not interrupting.

Getting this right is the difference between "feels human" and "keeps
cutting me off." Vapi and LiveKit both do this; this module is the
open-source equivalent.

The classifier is pure-string first (regex, cheap). An LLM slow-path is
optional — for a real phone call the 100ms of an LLM classify on a
partial hypothesis is often too slow, so the default is regex-only.
"""
from __future__ import annotations

import re
from enum import Enum


class BargeAction(str, Enum):
    IGNORE = "ignore"         # empty or noise
    CONTINUE = "continue"     # backchannel — keep talking
    INTERRUPT = "interrupt"   # real interruption — stop and listen


# Words/phrases that indicate acknowledgment, NOT interruption. If the
# caller says only one of these, don't stop speaking.
_BACKCHANNEL_TOKENS = frozenset({
    "mm", "mhm", "mmhm", "mm-hm", "hm", "hmm",
    "uh-huh", "uhhuh", "ah-huh",
    "yeah", "yea", "yep", "yup", "ya",
    "yes", "yeah!", "yes!", "right", "right.", "sure", "ok", "okay", "kk",
    "gotcha", "got it", "understood",
    "cool", "nice",
    "true", "totally",
    "ah", "oh", "aha", "oh ok", "oh okay",
    "wow", "huh",
})

# Explicit interruption cues. Even partial hypotheses matching these
# should IMMEDIATELY trigger interrupt.
_INTERRUPT_PATTERNS = [
    re.compile(r"\b(?:stop|wait|hold on|hang on)\b", re.I),
    re.compile(r"\b(?:actually|but wait|let me|can i|excuse me)\b", re.I),
    re.compile(r"\b(?:cancel|nevermind|never mind|change that|no wait)\b", re.I),
    re.compile(r"\b(?:you're wrong|that's wrong|not right|incorrect|mistake)\b", re.I),
    re.compile(r"^(?:no|nope|nah)[\s.,!?]*$", re.I),  # solo "no" is interrupt
    re.compile(r"\b(?:speak louder|speak up|repeat|say again)\b", re.I),
]


def _normalize(text: str) -> str:
    return (text or "").strip().lower().rstrip(".!?,")


def classify_barge(text: str) -> BargeAction:
    """Decide whether caller speech during agent TTS is a real interruption.

    - Empty / whitespace-only → IGNORE
    - Single backchannel token → CONTINUE
    - Contains an interrupt cue OR is > ~4 words → INTERRUPT
    """
    norm = _normalize(text)
    if not norm:
        return BargeAction.IGNORE

    if norm in _BACKCHANNEL_TOKENS:
        return BargeAction.CONTINUE

    # Multi-word backchannel like "yeah yeah" or "mhm mhm"
    tokens = norm.split()
    if len(tokens) <= 2 and all(t in _BACKCHANNEL_TOKENS for t in tokens):
        return BargeAction.CONTINUE

    # Explicit interrupt cue?
    for pattern in _INTERRUPT_PATTERNS:
        if pattern.search(text):
            return BargeAction.INTERRUPT

    # Anything longer than ~4 words is a real utterance, not a backchannel.
    if len(tokens) > 4:
        return BargeAction.INTERRUPT

    # Short (1-4 word) non-backchannel utterance: treat as interrupt
    # (conservative — we'd rather stop than talk over). Better UX for a
    # caller who says something short and specific like "different time?".
    return BargeAction.INTERRUPT


def should_interrupt(text: str) -> bool:
    """Convenience shortcut."""
    return classify_barge(text) is BargeAction.INTERRUPT


# ── LiveKit-style min-interruption gating (2026-08-29) ──────────────
#
# LiveKit's Agents SDK ships two knobs (`min_interruption_words`,
# `min_interruption_duration`) that suppress micro-interruptions:
# coughs, brief "uhh", one-word restarts.  The old classify_barge() had
# no timing signal and no configurable word floor, so a caller clearing
# their throat mid-sentence would fire an interrupt every time.
#
# `BargeInPolicy` layers on top of classify_barge():
#   1. Explicit interrupt patterns ("stop", "wait") always fire —
#      regardless of word count or duration.  Latency matters more than
#      false positives on those.
#   2. Otherwise, require BOTH ≥min_words AND ≥min_duration_ms of
#      speech before honoring an interrupt.  Below either threshold →
#      CONTINUE (agent keeps speaking) and downstream logs a
#      min_words_not_met barge-in event so we can tune from real calls.
#
# Defaults chosen to match LiveKit's shipping values (min_words=2,
# min_duration_ms=500) — safe starting point for a receptionist that
# needs to hear "wait" but not "uh".


from dataclasses import dataclass


@dataclass(frozen=True)
class BargeInPolicy:
    """Configuration for min-word/min-duration barge-in gating.

    Fields:
      min_interruption_words: minimum non-backchannel token count
        required before a barge-in commits.  Explicit interrupt cues
        (stop/wait/hold on/etc) BYPASS this floor.
      min_interruption_duration_ms: minimum speech duration before
        a barge-in commits.  Same explicit-cue bypass.
      trust_explicit_cues: if False, even explicit cues respect the
        min-floor thresholds (rarely wanted; kept for A/B testing).
    """
    min_interruption_words: int = 2
    min_interruption_duration_ms: int = 500
    trust_explicit_cues: bool = True

    def evaluate(
        self,
        text: str,
        duration_ms: int = 0,
    ) -> tuple[BargeAction, str]:
        """Classify a barge attempt against this policy.

        Returns (action, reason) where reason is a short trace string
        the observability layer emits.
        """
        base = classify_barge(text)
        if base in (BargeAction.IGNORE, BargeAction.CONTINUE):
            # Silence + backchannels already handled correctly.
            return base, f"base:{base.value}"
        # base == INTERRUPT — apply the min floors.
        norm = _normalize(text)
        tokens = norm.split()
        word_count = len(tokens)
        # Explicit-cue bypass: real interruption words trump the
        # min floors.
        has_explicit = self.trust_explicit_cues and any(
            p.search(text) for p in _INTERRUPT_PATTERNS
        )
        if has_explicit:
            return BargeAction.INTERRUPT, "explicit_cue"
        # Gate on word count.
        if word_count < self.min_interruption_words:
            return (
                BargeAction.CONTINUE,
                f"min_words_not_met:{word_count}<"
                f"{self.min_interruption_words}",
            )
        # Gate on duration when we have a real signal.  duration_ms==0
        # means the caller didn't report a duration; we don't invent
        # rejection there.
        if (
            duration_ms > 0
            and duration_ms < self.min_interruption_duration_ms
        ):
            return (
                BargeAction.CONTINUE,
                f"min_duration_not_met:{duration_ms}<"
                f"{self.min_interruption_duration_ms}",
            )
        return BargeAction.INTERRUPT, "policy_pass"


DEFAULT_BARGE_POLICY = BargeInPolicy()

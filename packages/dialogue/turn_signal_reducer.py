"""TurnSignalReducer — populate ConversationDecisionState from real signals.

2026-08-26 (ChatGPT audit H-P0.2): AcknowledgmentKind is shipped but the
runtime never populates `caller_shared_hardship / caller_corrected_us /
caller_is_dictating / caller_asked_to_wait / last_ack` on the decision
state.  Result: `_select_ack` always sees empty caller-state and falls
back to canonical acks — a strict improvement over prompt-only ACK
choice but not the humanness win we want.

This reducer closes that gap.  Regex + keyword based, deterministic,
never raises, ~5ms per turn worst case.  Not a full sentiment / NER
pass — those are downstream models.  This is the load-bearing signal
detection so the ACK selector can do the right thing.

**Design contract:**
- Input: last caller utterance + minimal context (previous agent turn,
  bounded transcript history for dictation detection).
- Output: a dict that populates `ConversationDecisionState` fields.
- Never raises.  Bad input → all-False signals → policy falls to
  canonical acks (safe default).
- Not a scoring model — first-match semantics.  Debuggable in prod
  logs via `TURN_SIGNAL_REDUCED signals=<...>` line.

**Not covered here (future work):**
- Affect inference (RUSHED / UPSET / ANXIOUS) — reducer's outputs feed
  policy but affect comes from acoustic features + prior extractor
  pass, not from text patterns.
- Multi-turn `caller_shared_hardship` decay — one turn ago the caller
  said "my tooth is killing me"; this turn they're picking a slot.
  Ideally hardship carries forward one turn. Reducer only reads the
  LAST caller turn — the caller for policy state.
- Slot-capture flag from actor.enter_slot_capture — that's a separate
  primitive; when we wire it, the reducer's `caller_is_dictating` will
  read the actor's slot-capture state directly instead of guessing
  from digits-in-transcript.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


# ── pattern libraries ──────────────────────────────────────────────
#
# Kept as module-level frozensets so they compile once at import.
# Case-normalized to lowercase; matcher normalizes input the same way.

# Hardship / pain / context sharing.  Matches concrete symptoms + emotional
# framing.  Not a medical dictionary — the audit called out "callers who
# share context deserve empathy" as the shape, not "detect every symptom".
_HARDSHIP_KEYWORDS: frozenset[str] = frozenset({
    # Physical symptoms
    "hurt", "hurts", "hurting",
    "pain", "painful", "aching", "achy",
    "killing me", "throbbing", "swollen", "swelling",
    "bleeding", "chipped", "cracked", "broke", "broken",
    "sensitive", "sore", "toothache", "sinus", "abscess",
    "infection", "infected", "burning",
    # Duration markers combined with distress
    "since monday", "since tuesday", "since wednesday",
    "since thursday", "since friday", "since yesterday",
    "for weeks", "for days", "for months",
    "getting worse", "worsening",
    # Emotional / life context
    "struggling", "stressed", "worried", "scared", "anxious",
    "afraid", "frustrated", "exhausted", "overwhelmed",
    "just lost", "just left", "just moved",
    "just found out", "divorce", "funeral",
    "father passed", "mother passed", "husband passed",
    "wife passed", "grandmother passed", "grandfather passed",
})

_HARDSHIP_PHRASES: tuple[re.Pattern, ...] = tuple(
    re.compile(rf"\b{p}\b", re.IGNORECASE) for p in (
        r"i'?ve been (having|dealing with|struggling)",
        r"it'?s been (hard|tough|rough|painful)",
        r"i can'?t (sleep|eat|chew|drink|open|breathe)",
        r"i'?m in (a lot of )?pain",
        r"i really need (help|to see someone|an appointment)",
    )
)


# Explicit correction phrases.  Caller says "no", "actually", or fixes
# a specific detail the agent got wrong.
#
# 2026-08-27: original patterns fired false-positives on ordinary
# "no <noun>" answers ("no pets", "no email please").  Tightened to
# require either an actual correction marker after `no` (comma,
# "I said/wasn't/didn't", "actually", etc.) or a self-standing
# correction word ("actually", "wait" at sentence start).
_CORRECTION_PHRASES: tuple[re.Pattern, ...] = tuple(
    re.compile(pattern, re.IGNORECASE) for pattern in (
        # "No," / "No." / "No —" — punctuation after 'no' is a real
        # correction signal.  Bare 'no <noun>' (no pets / no email)
        # doesn't match.
        r"^\s*no+\s*[,.\-—!]",
        # "No I said Thursday" / "No I didn't"
        r"^\s*no+\s+i\s+(said|didn'?t|did not|wasn'?t|meant)",
        # "Actually" as an opener
        r"^\s*actually[,.\s]",
        # "Wait" as an opener (correction, not literal request to wait —
        # that's the wait signal separately).
        r"^\s*wait[,.\-]",
        # Mid-sentence "I said X" — reasonably specific
        r"\bi said\s+\S+",
        # "not X, it's Y" — pure correction shape
        r"\bnot\s+\S+[,.\s]+it'?s?\s+\S+",
        # "I didn't say X" / "I did not say X"
        r"\bi (didn'?t|did not) say\b",
        # "That's wrong" / "You got it wrong"
        r"\bthat'?s wrong\b",
        r"\byou got it wrong\b",
        # "Actually it's 3:30" — actually as mid-sentence signal
        r"\bactually it'?s?\s+\S+",
    )
)


# Caller asked us to wait / hold.
_WAIT_PHRASES: tuple[re.Pattern, ...] = tuple(
    re.compile(pattern, re.IGNORECASE) for pattern in (
        r"\bhold on\b",
        r"\bhang on\b",
        r"\bgive me a (sec|second|moment|minute)\b",
        r"\bone (sec|second|moment|minute)\b",
        r"\blet me (check|grab|find|look|get)\b",
        r"\bjust a (sec|second|moment|minute)\b",
        r"\bwait a (sec|second|moment|minute)\b",
        r"\bhold please\b",
        r"\bbear with me\b",
    )
)


# Dictation heuristics — the reducer errs on the CAUTIOUS side.  False
# positive on dictation means the agent stays silent on a normal
# sentence (annoying but recoverable).  False negative means agent
# "gotcha"s between phone digits (patronizing, worse).
#
# Signal: string contains a run of digits with spaces / dashes /
# common English digit-words.
_DIGIT_WORDS: frozenset[str] = frozenset({
    "oh", "zero", "one", "two", "three", "four",
    "five", "six", "seven", "eight", "nine",
    "dash", "hyphen", "dot", "point",
})


@dataclass(frozen=True)
class ReducedSignals:
    """Structured output of the reducer.  Fields map 1:1 to the
    `ConversationDecisionState` boolean signal fields."""
    caller_shared_hardship: bool = False
    caller_corrected_us: bool = False
    caller_is_dictating: bool = False
    caller_asked_to_wait: bool = False
    # Why the reducer decided each way — surfaced for prod logs so we
    # can debug ACK selection off a real transcript.  Not part of the
    # ConversationDecisionState apply; consumed by log_signal_reducer.
    reasons: tuple[str, ...] = ()

    def to_state_kwargs(self) -> dict:
        """Return the subset of kwargs that go into ConversationDecisionState
        via `dataclasses.replace(state, **kwargs)`."""
        return {
            "caller_shared_hardship": self.caller_shared_hardship,
            "caller_corrected_us": self.caller_corrected_us,
            "caller_is_dictating": self.caller_is_dictating,
            "caller_asked_to_wait": self.caller_asked_to_wait,
        }


class TurnSignalReducer:
    """Read the last caller turn and derive the boolean signals that
    `ConversationDecisionState` needs for correct ACK selection.

    Stateless.  Instantiate once at brain construction (or module-level
    singleton — either way, no per-turn allocation).
    """

    def reduce(
        self,
        last_caller_text: str,
        *,
        last_agent_text: Optional[str] = None,
        transcript_history: Optional[list[str]] = None,
        slot_capture_active: bool = False,
    ) -> ReducedSignals:
        """Never raises.  Bad / empty input returns all-False signals.

        `slot_capture_active` is the actor's structured-slot flag (True
        when we're inside phone/name/email dictation mode).  When True,
        `caller_is_dictating` is forced True regardless of text — the
        actor knows better than the reducer's heuristic.
        """
        if not last_caller_text:
            return ReducedSignals()
        text = last_caller_text.strip()
        if not text:
            return ReducedSignals()
        try:
            reasons: list[str] = []
            hardship = self._detect_hardship(text, reasons)
            corrected = self._detect_correction(text, reasons)
            waiting = self._detect_wait(text, reasons)
            dictating = self._detect_dictation(
                text, slot_capture_active,
                last_agent_text or "", reasons,
            )
            return ReducedSignals(
                caller_shared_hardship=hardship,
                caller_corrected_us=corrected,
                caller_is_dictating=dictating,
                caller_asked_to_wait=waiting,
                reasons=tuple(reasons),
            )
        except Exception as e:
            # Defensive: return all-False on any regex / logic surprise.
            # The signal fields default False so policy falls back to
            # canonical acks — same behavior as if the reducer didn't
            # run at all.
            import logging as _l
            _l.getLogger(__name__).warning(
                "TurnSignalReducer.reduce raised %s: %r", type(e).__name__, e,
            )
            return ReducedSignals()

    # ── detectors ────────────────────────────────────────────────

    @staticmethod
    def _detect_hardship(text: str, reasons: list[str]) -> bool:
        low = text.lower()
        # Keyword scan (fast, catches most cases).
        for kw in _HARDSHIP_KEYWORDS:
            if kw in low:
                reasons.append(f"hardship_kw:{kw}")
                return True
        # Phrase scan (catches multi-word patterns keywords miss).
        for pat in _HARDSHIP_PHRASES:
            if pat.search(text):
                reasons.append(f"hardship_re:{pat.pattern[:40]}")
                return True
        return False

    @staticmethod
    def _detect_correction(text: str, reasons: list[str]) -> bool:
        for pat in _CORRECTION_PHRASES:
            if pat.search(text):
                reasons.append(f"correction_re:{pat.pattern[:40]}")
                return True
        return False

    @staticmethod
    def _detect_wait(text: str, reasons: list[str]) -> bool:
        for pat in _WAIT_PHRASES:
            if pat.search(text):
                reasons.append(f"wait_re:{pat.pattern[:40]}")
                return True
        return False

    @staticmethod
    def _detect_dictation(
        text: str,
        slot_capture_active: bool,
        last_agent_text: str,
        reasons: list[str],
    ) -> bool:
        # Actor knows best — if slot capture is active, dictating.
        if slot_capture_active:
            reasons.append("dictation:slot_capture")
            return True
        low = text.lower()
        # Signal 1: agent JUST asked for a structured slot.  When the
        # last agent utterance was clearly asking for a phone / name /
        # email / address, treat any caller reply that contains digits
        # or digit-words as dictation.
        agent_low = last_agent_text.lower() if last_agent_text else ""
        agent_asked_structured = any(
            phrase in agent_low
            for phrase in (
                "phone number", "your number", "your phone",
                "email", "spell", "your name",
                "address", "postcode", "zip code",
                "credit card", "social security",
            )
        )
        # Signal 2: content shape is digit-run or digit-word run.
        # Look at the token stream.  A "digit token" is either
        # a pure-digit string of any length OR a spoken digit word.
        # We don't cap length because callers often say phone numbers
        # as one long run ("5244772") — 7 chars but still one dictation
        # token.  The "at least one token with len>=4" gate below
        # separates real phone-shape input from casual "3 pm".
        tokens = [t for t in re.split(r"[\s,\-]+", low) if t]
        digit_like_tokens = sum(
            1 for t in tokens
            if t.isdigit() or t in _DIGIT_WORDS
        )
        # Isolated digit-run — most common phone-dictation case.
        # "0333 5244772" is 2 tokens both digit-runs; must catch.
        # Rule: 2+ tokens AND every token is digit-like AND at least
        # one is 4+ digits (rules out '3 pm').
        if (
            len(tokens) >= 2
            and digit_like_tokens == len(tokens)
            and any(t.isdigit() and len(t) >= 4 for t in tokens)
        ):
            reasons.append(
                f"dictation:isolated_digit_run tokens={len(tokens)}"
            )
            return True
        # General digit-heavy stream — 3+ tokens with majority digit-like.
        if len(tokens) >= 3 and digit_like_tokens >= len(tokens) // 2 + 1:
            reasons.append(
                f"dictation:digit_run tokens={len(tokens)} digits={digit_like_tokens}"
            )
            return True
        # If the agent asked for a structured slot AND the reply
        # contains ANY digits, treat as dictation (biased toward
        # cautious no-ack).  Not gated on the ratio because the caller
        # may be mid-utterance ("uh, five five five, and I don't remember
        # the rest") — better to stay silent than to inject an ack.
        if agent_asked_structured and (
            digit_like_tokens >= 1
            or any(c.isdigit() for c in text)
        ):
            reasons.append("dictation:structured_ask_with_digits")
            return True
        return False


# ── module-level singleton for the common case ─────────────────

_reducer = TurnSignalReducer()


def reduce_turn_signals(
    last_caller_text: str,
    *,
    last_agent_text: Optional[str] = None,
    transcript_history: Optional[list[str]] = None,
    slot_capture_active: bool = False,
) -> ReducedSignals:
    """Convenience wrapper for one-off use from brain.py / synthesizer.

    Uses a module-level singleton so no per-turn allocation.
    """
    return _reducer.reduce(
        last_caller_text,
        last_agent_text=last_agent_text,
        transcript_history=transcript_history,
        slot_capture_active=slot_capture_active,
    )


__all__ = ["TurnSignalReducer", "ReducedSignals", "reduce_turn_signals"]

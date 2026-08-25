"""Conversation-control fastpath.

A very small set of caller utterances are *deterministic* — the receptionist
knows the reply before the LLM does.  Sending "hello?", "can you hear me?",
"are you there?" through OpenAI wastes ~2.6s of network + generation time
for a reply that never varies.

This module maps normalized intents to canonical replies.  The reply text
is warmed into the TTS cache at server boot, so a fastpath hit is a ~2ms
disk read plus the wire time.

Design principles:
  1. Regex-free intent match against a small normalized set — fast + auditable
  2. Response cache's `normalize_input` deliberately strips "hi"/"hello" as
     leading fillers, so intents like "hello?" alone never survive that
     path.  This module handles that gap.
  3. Uses the same TTS cache the greeting warmup populates, so a hit is
     indistinguishable in the actor path from any other cached utterance.
  4. Canonical replies are short and voice-agent-appropriate.  If the
     business profile customizes the greeting, we still ship a generic
     "yep I hear you" — the tenant persona won't matter for hello/echo tests.

Public API:
    match_intent(text) -> Optional[str]        # returns canonical reply
    all_canonical_replies() -> list[str]        # for warmup
"""
from __future__ import annotations

import re
from typing import Optional


_LEADING_FILLERS = re.compile(
    r"^(?:um+|uh+|er+|hmm+|so|like|well)\s*[,.]?\s*",
    re.I,
)
_TRAILING_POLITENESS = re.compile(r"\s+(?:please|thanks|thank you)[.?!]*$", re.I)
_TRAILING_PUNCT = re.compile(r"[.?!,;:]+$")
# Kill internal punctuation that Deepgram inserts naturally in
# "Hello, can you hear me" without breaking phrase matches.
# 2026-08-21: added period. Flux writes "Hello. Can you hear me?"
# where Nova-3 wrote "Hello, can you hear me?" — the period broke
# fastpath matching, forcing every "hello can you hear me" through
# the LLM (verified on CAb499d5f, +1348ms first_token latency).
_INTERNAL_PUNCT = re.compile(r"[,;!?.]")
_WHITESPACE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Cheap normalization for intent lookup.

    Deliberately DIFFERENT from response_cache.normalize_input: we do NOT
    strip 'hi'/'hello'/'hey' as fillers, because those ARE the intent here.
    """
    if not text:
        return ""
    s = text.strip().lower()
    for _ in range(2):
        new = _LEADING_FILLERS.sub("", s)
        if new == s:
            break
        s = new
    s = _TRAILING_POLITENESS.sub("", s)
    s = _TRAILING_PUNCT.sub("", s)
    s = _INTERNAL_PUNCT.sub(" ", s)
    s = _WHITESPACE.sub(" ", s).strip()
    return s


# Intent → canonical reply.  Keys are the fully-normalized caller utterance.
# Same reply for the whole family so the TTS cache has one entry instead of N.
_HEARING_CHECK_REPLY = "Yep, I can hear you! How can I help?"
_GREETING_REPLY = "Hi there! How can I help you today?"

_INTENT_MAP: dict[str, str] = {
    # hearing-check family — the demo-killer utterance
    "can you hear me": _HEARING_CHECK_REPLY,
    "can you hear me now": _HEARING_CHECK_REPLY,
    "are you there": _HEARING_CHECK_REPLY,
    "are you listening": _HEARING_CHECK_REPLY,
    "hello can you hear me": _HEARING_CHECK_REPLY,
    "hi can you hear me": _HEARING_CHECK_REPLY,
    "hey can you hear me": _HEARING_CHECK_REPLY,
    "hello are you there": _HEARING_CHECK_REPLY,
    "hi are you there": _HEARING_CHECK_REPLY,
    "hey are you there": _HEARING_CHECK_REPLY,
    "do you hear me": _HEARING_CHECK_REPLY,
    "can you hear": _HEARING_CHECK_REPLY,
    "you there": _HEARING_CHECK_REPLY,

    # bare greeting family — no question, just "hello"
    "hello": _GREETING_REPLY,
    "hi": _GREETING_REPLY,
    "hey": _GREETING_REPLY,
    "hey there": _GREETING_REPLY,
    "hi there": _GREETING_REPLY,
    "hello there": _GREETING_REPLY,
    "howdy": _GREETING_REPLY,
    "yo": _GREETING_REPLY,
}


def match_intent(text: str) -> Optional[str]:
    """Return the canonical reply for a conversation-control intent, or
    None if the utterance isn't one of these deterministic patterns.

    Callers should invoke this BEFORE the response cache lookup — the
    response cache aggressively strips 'hello'/'hi' fillers and would
    normalize these utterances to empty.
    """
    normalized = _normalize(text)
    if not normalized:
        return None
    return _INTENT_MAP.get(normalized)


def all_canonical_replies() -> list[str]:
    """De-duplicated list of every canonical reply, for TTS warmup."""
    return sorted(set(_INTENT_MAP.values()))

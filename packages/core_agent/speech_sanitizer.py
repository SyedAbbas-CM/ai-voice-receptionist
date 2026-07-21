"""Sanitize LLM output before handing it to a TTS model.

Belt-and-suspenders for cases where the LLM ignores the "don't speak brackets"
rule in the system prompt. Called on every assistant reply text before TTS.

Rules (in order):
  1. Strip bracketed metadata:  "General consultation (30 min)" -> "General consultation"
     — parens, square brackets, angle brackets all removed.
  2. Strip tool-syntax leakage: "<lookup_answer>...</lookup_answer>", "tool: ...", etc.
  3. Normalize common TTS-mispronounced abbreviations:
     - "Dr." -> "Doctor"
     - "Mr." -> "Mister", "Mrs." -> "Missus", "Ms." -> "Miss"
     - "St." -> "Street" (only at end of address-y contexts)
     - "min" (bare, after a number) -> "minutes"
     - "hr"/"hrs" -> "hour"/"hours"
  4. Collapse whitespace, strip trailing space.

If the sanitizer leaves an empty string, we fall back to a safe canned line
so the caller never hears silence.
"""
from __future__ import annotations

import re


# Everything inside these bracket types gets removed entirely
_BRACKET_PATTERNS = [
    re.compile(r"\{[^{}]*\}"),       # curly-brace JSON objects — LLM sometimes emits
                                      # `{"date": "...", "service": "..."}` INSTEAD of
                                      # calling the tool. Caller shouldn't hear JSON.
    re.compile(r"\([^)]*\)"),        # parentheses
    re.compile(r"\[[^\]]*\]"),       # square brackets
    re.compile(r"<[^>]*>"),          # angle brackets — captures tool-call leaks too
]


# Tool-name leakage patterns (spoken form, without brackets)
_TOOL_LEAK_PATTERNS = [
    re.compile(r"\blookup[_ ]answer\b", re.I),
    re.compile(r"\bcheck[_ ]availability\b", re.I),
    re.compile(r"\bbook[_ ]appointment\b", re.I),
    re.compile(r"\bescalate[_ ]to[_ ]human\b", re.I),
    re.compile(r"\bbased on (?:the |our )?tool result\b", re.I),
    re.compile(r"\bthe (?:FAQ|database|system) (?:says|shows)\b", re.I),
]


# Abbreviations -> spoken words. Applied on WORD BOUNDARIES so we don't mangle
# things like "administrator" -> "adminisTratorer".
_ABBREVIATION_MAP = [
    (re.compile(r"\bDr\.?\s+", re.I), "Doctor "),
    (re.compile(r"\bMr\.?\s+"), "Mister "),
    (re.compile(r"\bMrs\.?\s+"), "Missus "),
    (re.compile(r"\bMs\.?\s+"), "Miss "),
    (re.compile(r"\bSt\.?\s+"), "Saint "),  # heuristic — "St. Louis" wins over "Main St."
    (re.compile(r"\b(\d+)\s*mins?\b", re.I), r"\1 minutes"),
    (re.compile(r"\b(\d+)\s*hrs?\b", re.I), r"\1 hours"),
    (re.compile(r"\b(\d+)\s*sec[s]?\b", re.I), r"\1 seconds"),
    (re.compile(r"\bapp\.?\b", re.I), "appointment"),
]


_FALLBACK_REPLY = "I'm sorry, could you say that again?"


def sanitize_for_speech(text: str) -> str:
    """Clean an LLM reply so it's safe to feed to a TTS model.

    Returns a fallback string if sanitization leaves nothing but whitespace.
    """
    if not text:
        return _FALLBACK_REPLY

    out = text
    # 1. Remove bracketed metadata FIRST so tool-name matchers below don't
    #    accidentally leave partial words behind.
    for pat in _BRACKET_PATTERNS:
        out = pat.sub("", out)

    # 2. Remove tool-name leakage in prose
    for pat in _TOOL_LEAK_PATTERNS:
        out = pat.sub("", out)

    # 3. Expand abbreviations
    for pat, repl in _ABBREVIATION_MAP:
        out = pat.sub(repl, out)

    # 4. Collapse whitespace and clean up dangling punctuation left by removals
    out = re.sub(r"\s+", " ", out)
    out = re.sub(r"\s+([,.!?;:])", r"\1", out)   # " ," -> ","
    out = re.sub(r"([,.!?;:]){2,}", r"\1", out)  # ",," -> ","
    out = out.strip(" \t\n,;:")

    return out or _FALLBACK_REPLY

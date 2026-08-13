"""Phone slot: Layer A (spoken-digit normalization).

R3 P0 (2026-08-13): the ONLY thing this module owns is turning caller
speech / DTMF into a canonical digit string.  It does NOT know what a
valid phone number looks like — that's Layer B's job
(`phone_validator.py`, which wraps Google's libphonenumber).

Layer A is region-agnostic.  "zero double three" → "033" is the same
whether we're in Karachi or Kansas.

Public API:
    normalize_spoken_digits(text)  -> canonical digit string
                                      (with optional leading "+")

Usage:
    canonical = normalize_spoken_digits("zero double three, one two")
    # -> "03312"

    canonical = normalize_spoken_digits("plus nine two three three three")
    # -> "+92333"

Design guardrails ChatGPT set (see docs/rnd-2026-08/57):
  - Keep Layer A separate from Layer B.
  - Never ask the LLM to count digits.
  - Never bake "10 digits" or "11 digits" into any prompt or regex.
"""
from __future__ import annotations

import re


# Words → digits.  Individual-digit context (phone-collection always is).
_DIGIT_WORDS: dict[str, str] = {
    "zero": "0", "oh": "0", "naught": "0", "nought": "0",
    "one": "1",
    "two": "2", "to": "2", "too": "2",
    "three": "3", "tree": "3",
    "four": "4", "for": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8", "ate": "8",
    "nine": "9",
}

# "double X" -> "XX", "triple X" -> "XXX", "quadruple X" -> "XXXX".
# Very common when giving Pakistani numbers or UK-style spellings.
_REPEAT_WORDS: dict[str, int] = {
    "double": 2, "dbl": 2,
    "triple": 3, "trpl": 3,
    "quadruple": 4, "quad": 4,
}

# `+` marker at the very start indicates E.164 form.
_PLUS_WORDS = {"plus", "+"}

# Filler words to drop entirely so they don't collide with digit words.
_STRIP_WORDS = {
    "and", "then", "next", "please", "thanks", "thank", "you",
    "uh", "um", "er", "the", "a", "an", "is", "it's", "its",
    "my", "number", "phone", "cell", "mobile", "digits", "digit",
    "so", "okay", "ok",
}


def normalize_spoken_digits(text: str) -> str:
    """Convert English-spoken digits into a canonical digit string.

    Preserves a leading `+` (E.164 marker) if the caller said "plus".
    Non-digit / non-plus content is dropped; only digits and one
    optional leading `+` are returned.

    Examples:
      "zero double three"           -> "0033"
      "0333"                        -> "0333"
      "plus nine two, three three"  -> "+9233"
      "5244 7724"                   -> "52447724"
      "triple three, one"           -> "3331"
      "oh three three three"        -> "0333"
      ""                            -> ""
      "hello there"                 -> ""

    The output is guaranteed to be a string matching:
        ^\\+?[0-9]*$
    """
    if not text:
        return ""

    # Lowercase + peel off common punctuation.  Keep `+` because it can
    # signal E.164.  Replace hyphens/commas/dots with spaces so word
    # tokenization works: "0-3-3" -> "0 3 3".
    s = text.lower()
    s = re.sub(r"[-,.;:_/\\()\[\]{}]+", " ", s)
    # Split "+923..." into "+ 923..." so the token loop can see the `+`
    # as its own token and set the E.164 flag.  Only at the FRONT.
    s = re.sub(r"^(\s*)\+", r"\1+ ", s)
    s = re.sub(r"\s+", " ", s).strip()

    tokens = s.split(" ")
    out: list[str] = []
    leading_plus = False
    i = 0
    while i < len(tokens):
        tok = tokens[i].strip()
        if not tok:
            i += 1
            continue

        # Explicit `+` or word "plus" at the front means E.164.
        if tok == "+" or (i == 0 and tok in _PLUS_WORDS):
            leading_plus = True
            i += 1
            continue
        # `plus` mid-string = filler.
        if tok in _PLUS_WORDS:
            i += 1
            continue

        # "double X" / "triple X" / "quadruple X"
        if tok in _REPEAT_WORDS and i + 1 < len(tokens):
            n = _REPEAT_WORDS[tok]
            nxt = tokens[i + 1]
            if nxt in _DIGIT_WORDS:
                out.append(_DIGIT_WORDS[nxt] * n)
                i += 2
                continue
            if nxt.isdigit() and len(nxt) == 1:
                out.append(nxt * n)
                i += 2
                continue
            # "double 12" is ambiguous; treat "double" as filler.
            i += 1
            continue

        if tok in _STRIP_WORDS:
            i += 1
            continue

        if tok in _DIGIT_WORDS:
            out.append(_DIGIT_WORDS[tok])
            i += 1
            continue

        # Fallback: strip non-digits from the token.  Handles "0333",
        # "5244", and salvages what we can from mixed junk.
        digits = re.sub(r"[^0-9]", "", tok)
        if digits:
            out.append(digits)
        i += 1

    body = "".join(out)
    if leading_plus and body:
        return "+" + body
    return body

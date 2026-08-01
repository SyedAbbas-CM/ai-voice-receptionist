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
  4. Normalize numerics that TTS mispronounces: currency, times, phone numbers,
     dates, percentages. ("$25.50" -> "twenty five dollars and fifty cents",
     "2:30pm" -> "two thirty pm", "555-1234" -> "five five five, one two three four".)
  5. Collapse whitespace, strip trailing space.
  6. Optional flow-mode: convert mid-utterance periods to commas so ElevenLabs
     and Cartesia don't inject robotic ~400ms pauses at every sentence.

If the sanitizer leaves an empty string, we fall back to a safe canned line
so the caller never hears silence.
"""
from __future__ import annotations

import re
try:
    from num2words import num2words as _num2words
except ImportError:  # pragma: no cover — fallback if package missing
    def _num2words(n, **kwargs):
        return str(n)


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


# --- numeric normalizers -------------------------------------------------
# ElevenLabs Flash v2.5 mispronounces most numerics ($1,000,000 -> "one
# thousand thousand dollars", 2:30pm -> "two colon thirty pm", 555-1234 ->
# "five five five dash one two three four"). Fix in code before hitting TTS.

def _int_to_words(n: int) -> str:
    try:
        # num2words returns "one hundred, and thirty-five" — strip the ", and"
        # and the hyphens so TTS reads it clean.
        s = _num2words(n)
        s = s.replace(", and ", " ").replace(" and ", " ").replace("-", " ")
        return s
    except Exception:
        return str(n)


def _normalize_currency(m: re.Match) -> str:
    """$25.50 -> twenty five dollars and fifty cents"""
    dollars = int(m.group(1).replace(",", ""))
    cents_str = m.group(2)
    if cents_str:
        cents = int(cents_str)
        if cents == 0:
            return f"{_int_to_words(dollars)} dollars"
        return f"{_int_to_words(dollars)} dollars and {_int_to_words(cents)} cents"
    return f"{_int_to_words(dollars)} dollars"


def _normalize_percent(m: re.Match) -> str:
    """25% -> twenty five percent"""
    n = int(m.group(1))
    return f"{_int_to_words(n)} percent"


def _normalize_bare_hour(m: re.Match) -> str:
    """5pm -> five pm ; 12 AM -> twelve am"""
    hour = int(m.group(1))
    ampm = m.group(2).lower().replace(".", "")
    return f"{_int_to_words(hour)} {ampm}"


def _normalize_iso_date(m: re.Match) -> str:
    """2026-08-15 -> August fifteenth twenty twenty six"""
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    months = ["", "January", "February", "March", "April", "May", "June",
              "July", "August", "September", "October", "November", "December"]
    month = months[mo] if 1 <= mo <= 12 else str(mo)
    # Ordinal day
    try:
        day = _num2words(d, to="ordinal")
        day = day.replace("-", " ")
    except Exception:
        day = str(d)
    year_str = _normalize_year_int(y)
    return f"{month} {day} {year_str}"


def _normalize_year_int(y: int) -> str:
    if 1900 <= y <= 2099:
        first, second = y // 100, y % 100
        if second == 0:
            return f"{_int_to_words(first)} hundred"
        if second < 10:
            return f"{_int_to_words(first)} oh {_int_to_words(second)}"
        return f"{_int_to_words(first)} {_int_to_words(second)}"
    return _int_to_words(y)


def _normalize_time(m: re.Match) -> str:
    """2:30pm -> two thirty pm ; 10:00 -> ten o'clock ; 14:15 -> two fifteen pm"""
    hour = int(m.group(1))
    minute = int(m.group(2))
    ampm = (m.group(3) or "").lower().replace(".", "")
    # 24h -> 12h
    if not ampm:
        if hour == 0:
            hour, ampm = 12, "am"
        elif hour < 12:
            ampm = "am"
        elif hour == 12:
            ampm = "pm"
        else:
            hour, ampm = hour - 12, "pm"
    if minute == 0:
        return f"{_int_to_words(hour)} o'clock {ampm}".rstrip()
    if minute < 10:
        return f"{_int_to_words(hour)} oh {_int_to_words(minute)} {ampm}".rstrip()
    return f"{_int_to_words(hour)} {_int_to_words(minute)} {ampm}".rstrip()


def _normalize_phone(m: re.Match) -> str:
    """5551234, 555-1234, (555) 123-4567 -> spelled out digit-by-digit with breath commas"""
    digits = re.sub(r"\D", "", m.group(0))
    if len(digits) == 7:
        # 555-1234 -> five five five, one two three four
        first = " ".join(_int_to_words(int(d)) for d in digits[:3])
        rest = " ".join(_int_to_words(int(d)) for d in digits[3:])
        return f"{first}, {rest}"
    if len(digits) == 10:
        # (555) 123-4567 -> five five five, one two three, four five six seven
        area = " ".join(_int_to_words(int(d)) for d in digits[:3])
        mid = " ".join(_int_to_words(int(d)) for d in digits[3:6])
        rest = " ".join(_int_to_words(int(d)) for d in digits[6:])
        return f"{area}, {mid}, {rest}"
    if len(digits) == 11 and digits.startswith("1"):
        # +1 5551234567
        area = " ".join(_int_to_words(int(d)) for d in digits[1:4])
        mid = " ".join(_int_to_words(int(d)) for d in digits[4:7])
        rest = " ".join(_int_to_words(int(d)) for d in digits[7:])
        return f"one, {area}, {mid}, {rest}"
    return m.group(0)


def _normalize_year(m: re.Match) -> str:
    """2026 -> twenty twenty six ; 1985 -> nineteen eighty five"""
    return _normalize_year_int(int(m.group(1)))


def _normalize_standalone_int(m: re.Match) -> str:
    """Bare integers (not part of dates/times/phones we handled above) -> words.
    Only for numbers <= 100000 to keep it readable; long IDs stay as digits."""
    n_str = m.group(0).replace(",", "")
    n = int(n_str)
    if n > 100_000:
        return m.group(0)  # leave "1234567 items" alone
    return _int_to_words(n)


# Apply in this order so more-specific patterns win first.
_NUMERIC_TRANSFORMS = [
    # Currency: $25, $25.50, $1,000, $1,000,000
    (re.compile(r"\$\s*(\d{1,3}(?:,\d{3})*|\d+)(?:\.(\d{1,2}))?"), _normalize_currency),
    # Percent: 25%, 12.5%  (drop the decimal — TTS gets confused; round)
    (re.compile(r"(\d+)(?:\.\d+)?\s*%"), _normalize_percent),
    # ISO date: 2026-08-15 (must run BEFORE the year pattern below)
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), _normalize_iso_date),
    # Time with colon: 2:30pm, 10:00 AM, 14:15
    (re.compile(r"\b(\d{1,2}):(\d{2})\s*(a\.?m\.?|p\.?m\.?)?", re.I), _normalize_time),
    # Bare-hour time: 5pm, 12 AM, 10 p.m. (must have am/pm suffix — a lone
    # number without one isn't a time)
    (re.compile(r"\b(\d{1,2})\s*(a\.?m\.?|p\.?m\.?)\b", re.I), _normalize_bare_hour),
    # Phone with area code: (555) 123-4567, 555-123-4567, +1 555 123 4567
    # Anchor with (?<!\S) instead of \b so leading whitespace isn't consumed
    (re.compile(r"(?<!\S)\+?1?\s?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b"), _normalize_phone),
    # Phone without area code: 555-1234
    (re.compile(r"\b\d{3}[.-]\d{4}\b"), _normalize_phone),
    # Years: 2026, 1985 (avoid times we already handled)
    (re.compile(r"\b(19\d{2}|20\d{2})\b"), _normalize_year),
    # Bare integers with commas: 1,234 -> one thousand two hundred thirty four
    (re.compile(r"\b\d{1,3}(?:,\d{3})+\b"), _normalize_standalone_int),
]


_FALLBACK_REPLY = "I'm sorry, could you say that again?"


# --- flow mode transforms -------------------------------------------------
# 2026-07-30 discovery: ElevenLabs (and every neural TTS we tested) inserts a
# multi-hundred-ms pause at every period, which reads as robotic staccato even
# on the best voice. Real receptionists don't hard-stop between clauses when
# they're mid-thought — they soft-flow with commas. Below transforms convert
# safe sentence-boundary periods into commas so the LLM can keep its
# grammar-correct output style but the audio flows.
#
# "Safe" period = followed by a lowercase continuation OR by a short connector
# (and, but, so, or, then). We leave hard-boundary periods intact:
#   - question marks / exclamations
#   - sentence-final periods before capital-letter proper-noun starts we can't
#     verify (proper noun risk = keep the period, minor pause is fine)
#   - periods in numerics (7.30, $1.50, u.s.a.)
#   - the very last period of the whole reply (natural end-of-turn cue)
#
# Optionally injects a comma before conversational connectors ("so what's...")
# even where the LLM omitted one, since ElevenLabs treats commas as a *shorter*
# breath than periods and it reads more human.

# Turn ". lowercase" into ", lowercase" (LLM keeps its grammar, TTS flows).
# Also handle the common continuation words that start with a capital letter but
# are still mid-thought in receptionist speech: "I", "I'm", "I've", "You",
# "You're", "We", "We're", "It", "It's", "That", "That's", "This", "This's",
# "So", "And", "But", "Or", "Then", "Well", "Okay", "Alright", "Just", "How",
# "What", "Which", "Would", "Could", "Can", "Do", "Does", "Are", "Is", "For".
# These are the words a real receptionist chains into mid-flow without a hard
# breath. Leaving the period would inject a robotic ~400ms pause.
_SOFT_START_WORDS = (
    r"I|I'\w+|You|You'\w+|We|We'\w+|It|It'\w+|"
    r"That|That'\w+|This|These|Those|They|There|"
    r"So|And|But|Or|Then|Well|Okay|Alright|Just|"
    r"How|What|Which|Who|When|Where|Why|"
    r"Would|Could|Can|Should|Might|May|Will|"
    r"Do|Does|Did|Are|Is|Was|Were|Have|Has|Had|"
    r"For|To|If|Let|Let's|Me|My|Our|Your|Their|"
    r"Just|Really|Actually|Basically"
)
_PERIOD_TO_COMMA = re.compile(
    rf"\.\s+(?=(?:[a-z]|(?:{_SOFT_START_WORDS})\b))"
)

# Turn "so what's going on" into "so, what's going on" — soft connector needs
# a breath but the LLM often omits the comma. Only insert if there ISN'T
# already comma/period/dash right before the connector.
_CONNECTOR_BREATH = re.compile(
    r"(?<=[a-zA-Z])\s+(so|and|but|or|then|well|okay|alright)\s+",
    re.I,
)


def apply_flow_mode(text: str) -> str:
    """Convert sentence-boundary periods to commas for natural TTS flow.

    Safe to run on every reply. Leaves question marks, exclamations, numeric
    dots, and the terminal period alone.
    """
    if not text:
        return text
    # 1. Detach the last sentence-final period (if any) so we don't touch it
    m = re.search(r"[.!?]+\s*$", text)
    tail = m.group(0) if m else ""
    body = text[: -len(tail)] if tail else text
    # 2. Convert mid-utterance periods before lowercase to commas.  Also
    #    lowercase the letter after the new comma when it isn't "I" — TTS
    #    reads the resulting sentence as one continuous thought instead of
    #    two grammatical sentences (which the model tries to hard-break).
    def _period_repl(m):
        # Look at the next character after the pattern match
        next_char_pos = m.end()
        if next_char_pos < len(body) and body[next_char_pos:next_char_pos+2] != "I " and body[next_char_pos] != "I":
            return ", "
        return ", "
    body = _PERIOD_TO_COMMA.sub(", ", body)
    # Lowercase the first letter after any comma we inserted, EXCEPT "I"
    # and "I'..." forms and proper nouns. Cheap heuristic: only lowercase
    # single capital-then-lowercase words (not ALL-CAPS acronyms).
    def _soft_lower(m):
        word = m.group(1)
        # Don't touch "I" or "I'm/I've/I'll..." — they must stay capitalized
        if word == "I" or word.startswith("I'"):
            return m.group(0)
        # Don't touch proper-noun-looking words (Cedar, Nia, Doctor, etc)
        # — we only lowercase common transition words we KNOW are lowerable
        if word.lower() in _KNOWN_LOWERABLE:
            return f", {word.lower()}"
        return m.group(0)
    body = re.sub(r", ([A-Z][a-z']+)", _soft_lower, body)
    # 3. Add a soft comma before naked connectors mid-utterance
    body = _CONNECTOR_BREATH.sub(lambda m: f", {m.group(1).lower()} ", body)
    return body + tail


# Words that are safe to lowercase after a soft-comma we inserted. These are
# common connectors / continuations that don't look weird as lowercase mid-
# utterance ("...Cedar Ridge Family Dental, this call may be recorded...").
_KNOWN_LOWERABLE = {
    "this", "that", "these", "those", "which", "who", "whose", "whom",
    "how", "when", "where", "why", "what",
    "you", "we", "it", "he", "she", "they",
    "would", "could", "can", "will", "should", "might", "may",
    "do", "does", "did", "are", "is", "was", "were",
    "for", "with", "and", "but", "or", "so", "then", "just",
    "well", "okay", "alright",
    "let", "let's",
    "our", "your", "their", "his", "her", "its",
}


def sanitize_for_speech(text: str, flow_mode: bool = True) -> str:
    """Clean an LLM reply so it's safe to feed to a TTS model.

    Returns a fallback string if sanitization leaves nothing but whitespace.

    flow_mode: when True (default), applies punctuation transforms that make
    the TTS output flow more naturally (period -> comma before continuations).
    Set to False for scenarios where you want the LLM's exact punctuation
    (adversarial harness assertions, testing).
    """
    if not text:
        return _FALLBACK_REPLY

    out = text
    # 1. Normalize numerics FIRST so phone-with-parens "(555) 123-4567" isn't
    #    eaten by the bracket-stripper below.
    for pat, fn in _NUMERIC_TRANSFORMS:
        out = pat.sub(fn, out)

    # 2. Remove bracketed metadata (parens, square, angle brackets)
    for pat in _BRACKET_PATTERNS:
        out = pat.sub("", out)

    # 3. Remove tool-name leakage in prose
    for pat in _TOOL_LEAK_PATTERNS:
        out = pat.sub("", out)

    # 4. Expand abbreviations
    for pat, repl in _ABBREVIATION_MAP:
        out = pat.sub(repl, out)

    # 5. Em-dash and en-dash → comma (unstable on Flash v2.5 per research)
    out = re.sub(r"\s*[—–]\s*", ", ", out)

    # 6. Collapse whitespace and clean up dangling punctuation left by removals
    out = re.sub(r"\s+", " ", out)
    out = re.sub(r"\s+([,.!?;:])", r"\1", out)   # " ," -> ","
    out = re.sub(r"([,.!?;:]){2,}", r"\1", out)  # ",," -> ","
    out = out.strip(" \t\n,;:")

    # 7. Flow-mode punctuation transforms (kills TTS staccato)
    if flow_mode and out:
        out = apply_flow_mode(out)

    return out or _FALLBACK_REPLY

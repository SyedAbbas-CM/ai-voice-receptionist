"""Slot-capture LLM prompt discipline (LiveKit steal #7, 2026-08-29).

Adapted from LiveKit's beta/workflows/phone_number.py.  Their pattern:
when a slot needs capture, install a NARROW sub-agent prompt that
knows nothing about the wider conversation — only how to capture
this one slot cleanly.  The wider prompt resumes after the slot lands.

Why we're stealing this:
  * BUG-CHR-01 root cause was the wide receptionist prompt trying to
    do everything at once — booking + persona + phone capture + FAQ +
    intent resolution.  When it hit an unknown-shape phone (Dutch
    +31 format), gpt-4o-mini returned an empty completion.
  * A narrow phone-only prompt gives the model a bounded problem
    with clear rules ("call the tool at first hypothesis," "read back
    in groups," "don't invent digits").
  * Also lets us switch to a cheaper/faster model for slot-capture
    turns (Groq allam-2-7b handles narrow-scope prompts fine and
    cuts TTFT by ~300ms).

This module ONLY produces prompt text.  Wiring into
StructuredInputSession + actor happens in the follow-up commit.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional


# ── phone-slot prompts (adapted from LK phone_number.py) ────


_PHONE_BASE_INSTRUCTIONS = """\
You are one narrow step in a broader conversation, responsible only for capturing a phone number from the caller.

{modality_specific}

Rules:
1. Call `update_phone_number` at the FIRST hypothesis of a full or partial number. Do it BEFORE asking any questions. Update again on every correction.
2. Never invent digits. Stick strictly to what the caller said.
3. {confirmation_instructions}
4. If the number is unclear after 2 exchanges, prompt for it in parts: first the country/area code, then the remaining digits.
5. Never repeat the phone number back as a solid block of digits. Always read it in groups — for a 10-digit US number: "five five five, one two three, four five six seven." For a Dutch number: "zero six, two five, zero zero seven six zero zero."
6. Ignore off-topic input. Do not answer FAQ, do not book, do not transfer. If the caller asks something unrelated, briefly acknowledge and steer back: "Got it — I'll come back to that. Right now I just need your number."
7. No markdown, no filler openings ("Sure!", "Absolutely!"), no example phone numbers or format explanations unless the caller asked.
8. Always invoke the tool explicitly. Do NOT simulate tool use — no work happens without a real tool call.
{extra_instructions}"""


_PHONE_AUDIO_MODALITY = """\
Handle input as noisy voice transcription. Callers say phone numbers in many shapes:
- "five five five, one two three, four five six seven"
- "555 123 4567" / "555-123-4567" / "(555) 123-4567"
- "plus one, five five five, one two three, four five six seven"
- "area code five five five, then one two three four five six seven"
- Dutch: "nul zes, twee vijf, nul nul, zeven zes, nul nul" (06 25 00 76 00)
- German: "null eins, sieben eins, ..."

Silently normalize:
- Spoken digits → numeric ("five" → 5, "zero" or "oh" → 0, "nul" → 0 in Dutch, "null" → 0 in German).
- Strip filler words, hesitations, dashes, parentheses.
- "plus" or "double-oh" at the start → international prefix `+` or `00`.
- "area code" is a prefix, not part of the number.

Do NOT mention the normalization. Do NOT correct the caller aloud."""


_PHONE_TEXT_MODALITY = """\
Handle input as typed text. The caller will type digits directly.

Silently normalize:
- Strip whitespace, dashes, parentheses, dots.
- If digits are grouped in an unusual way, clean up silently.
- Do NOT mention corrections."""


_PHONE_CONFIRM_ON = (
    "After you call `update_phone_number` and the tool result confirms a valid number, "
    "read it back to the caller in groups and ask them to confirm. Once they confirm, "
    "call `confirm_phone_number`. Until confirmed, keep listening for corrections."
)


_PHONE_CONFIRM_OFF = (
    "You do NOT need to ask the caller to confirm. Once you call `update_phone_number` "
    "and the tool accepts it as valid, the capture is complete and the wider conversation resumes."
)


# ── data classes ─────────────────────────────────────────────


Modality = Literal["audio", "text"]


@dataclass(frozen=True)
class SlotCapturePrompt:
    """One narrow sub-agent prompt for a specific slot capture.

    - `instructions` is the system-note the actor injects for the
      duration of the capture.
    - `on_enter_prompt` is a one-shot user-visible open ("What's the
      best number to reach you at?") the LLM speaks when it first
      enters the sub-agent state.
    - `tools_hint` lists the tool names the LLM MUST use.  Actor
      installs corresponding function-tool schemas for these.
    """
    instructions: str
    on_enter_prompt: str
    tools_hint: tuple[str, ...]


def build_phone_capture_prompt(
    modality: Modality = "audio",
    require_confirmation: bool = True,
    extra_instructions: str = "",
    on_enter_persona_hint: str = "",
) -> SlotCapturePrompt:
    """Build the phone-slot sub-agent prompt.

    Args:
      modality: "audio" (Twilio Media Streams / voice widget) or
        "text" (SMS / chat channel).  Selects the modality-specific
        normalization block.
      require_confirmation: when True, LLM must call
        confirm_phone_number after read-back.  Default True on audio
        (STT noise makes read-back valuable), safe to disable on text.
      extra_instructions: appended verbatim after the base rules.
        Use for tenant-specific business rules ("we prefer mobile
        over landline") or vertical-specific ("dental patient may
        prefer parent's number if they're a minor").
      on_enter_persona_hint: brief instruction for the FIRST spoken
        line so it matches the wider agent's persona ("Say 'What's
        the best number to reach you at?' in a warm, casual tone").
    """
    modality_block = (
        _PHONE_AUDIO_MODALITY if modality == "audio"
        else _PHONE_TEXT_MODALITY
    )
    confirmation_block = (
        _PHONE_CONFIRM_ON if require_confirmation
        else _PHONE_CONFIRM_OFF
    )
    extra = f"\n{extra_instructions.strip()}" if extra_instructions else ""

    instructions = _PHONE_BASE_INSTRUCTIONS.format(
        modality_specific=modality_block,
        confirmation_instructions=confirmation_block,
        extra_instructions=extra,
    )

    on_enter = (
        on_enter_persona_hint.strip()
        or "Ask the caller for the best phone number to reach them at, briefly and warmly."
    )

    tools = (
        ("update_phone_number", "confirm_phone_number",
         "decline_phone_number_capture")
        if require_confirmation
        else ("update_phone_number", "decline_phone_number_capture")
    )
    return SlotCapturePrompt(
        instructions=instructions,
        on_enter_prompt=on_enter,
        tools_hint=tools,
    )


# ── read-back helpers ────────────────────────────────────────


def read_back_in_groups(digits: str, group_sizes: Optional[list[int]] = None) -> str:
    """Format a digit string for spoken read-back.

    Default groupings tuned for common E.164 shapes:
      - 10 digits (US NANP): 3-3-4  "555, 123, 4567"
      - 11 digits with leading 1 (US+): "1, 555, 123, 4567"
      - 10 digits starting 0 (Dutch mobile 06): "06, 25, 00, 76, 00"
      - anything else: 2-digit groups, plus optional country prefix

    Never raises.  If digits is empty, returns "".
    """
    if not digits:
        return ""
    d = "".join(c for c in digits if c.isdigit() or c == "+")
    if not d:
        return ""
    prefix = ""
    if d.startswith("+"):
        prefix = d[:2]
        d = d[2:]
    if group_sizes is None:
        n = len(d)
        if n == 10 and d.startswith("0"):
            # Dutch mobile / EU cellular shape 06-XX-XX-XX-XX
            group_sizes = [2, 2, 2, 2, 2]
        elif n == 10:
            # US NANP without country code
            group_sizes = [3, 3, 4]
        elif n == 11 and d.startswith("1"):
            # US NANP with leading 1
            group_sizes = [1, 3, 3, 4]
        else:
            # Fallback: 2-digit groups.  Works for +49, +33, +351, etc.
            group_sizes = [2] * (n // 2)
            if n % 2:
                group_sizes.append(n % 2)
    parts: list[str] = []
    i = 0
    for gs in group_sizes:
        if i >= len(d):
            break
        parts.append(d[i:i + gs])
        i += gs
    if i < len(d):
        parts.append(d[i:])
    out = ", ".join(parts)
    if prefix:
        out = f"{prefix}, {out}"
    return out


# ── email-slot prompt (task #142, 2026-08-30) ────────────────


_EMAIL_BASE_INSTRUCTIONS = """\
You are one narrow step in a broader conversation, responsible only for capturing an email address from the caller.

{modality_specific}

Rules:
1. Call `update_email` at the FIRST hypothesis of a complete address (something with an '@' and a '.').  Update again on every correction.
2. Never invent an address.  If you missed a word, ask them to repeat that part.
3. If the caller says the domain sounds off (gmial, hotmial, yahooo, etc.) the tool will suggest a correction — read back the SUGGESTED version and confirm before committing.
4. Never spell the address back as one long string.  Break it up: local part, then 'at', then domain, then 'dot', then TLD.
5. Ignore off-topic input.  If the caller asks something unrelated, briefly acknowledge and steer back: "I'll come back to that — right now I just need your email."
6. No markdown, no filler openings, no example emails.
7. Always invoke the tool explicitly.  Do NOT simulate tool use.
{extra_instructions}"""


_EMAIL_AUDIO_MODALITY = """\
Handle input as noisy voice transcription.  Callers say emails in many shapes:
- "john dot smith at gmail dot com"
- "j-o-h-n at yahoo dot com" (spellback letter by letter)
- "my email is john underscore smith at hotmail dot com"

Silently normalize:
- "dot" or "period" → "."
- "at" → "@"
- "dash" or "hyphen" → "-"
- "underscore" → "_"
- Spelled letters (j-o-h-n) → joined ("john")
- Strip filler words ("my email is", "the address is", "it's")

Do NOT mention the normalization."""


_EMAIL_TEXT_MODALITY = """\
Handle input as typed text.  Strip whitespace + trailing punctuation."""


def build_email_capture_prompt(
    modality: Modality = "audio",
    extra_instructions: str = "",
    on_enter_persona_hint: str = "",
) -> SlotCapturePrompt:
    """Build the email-slot sub-agent prompt.  Mirrors phone prompt
    shape.  Confirmation is IMPLICIT via read-back — no separate
    confirm tool needed for email; the tool suggests corrections
    for typos, and the LLM asks the caller to confirm the suggested
    correction before committing."""
    modality_block = (
        _EMAIL_AUDIO_MODALITY if modality == "audio"
        else _EMAIL_TEXT_MODALITY
    )
    extra = f"\n{extra_instructions.strip()}" if extra_instructions else ""
    instructions = _EMAIL_BASE_INSTRUCTIONS.format(
        modality_specific=modality_block,
        extra_instructions=extra,
    )
    on_enter = (
        on_enter_persona_hint.strip()
        or "Ask the caller for the best email to send a confirmation to, briefly and warmly."
    )
    return SlotCapturePrompt(
        instructions=instructions,
        on_enter_prompt=on_enter,
        tools_hint=(
            "update_email", "decline_email_capture",
        ),
    )


# ── name-slot prompt ─────────────────────────────────────


_NAME_BASE_INSTRUCTIONS = """\
You are one narrow step in a broader conversation, responsible only for capturing the caller's name.

{modality_specific}

Rules:
1. Call `update_name` at the FIRST hypothesis of a name.  Update again on every correction.
2. Never invent a name.  If STT is garbled, ask the caller to spell the last name.
3. If you only heard a first name, ask "and last name?" — most bookings need both.
4. Never treat 'null', 'none', 'user', 'test', 'the caller', or similar generic strings as a real name — those are LLM pollution.  Re-ask.
5. Ignore off-topic input.  Steer back: "Got it — I'll come back to that.  What name should the booking be under?"
6. No markdown, no filler openings.
7. Always invoke the tool explicitly.
{extra_instructions}"""


_NAME_AUDIO_MODALITY = """\
Handle input as noisy voice transcription.  Callers give names in many shapes:
- "John Smith"
- "my name is John Smith"
- "This is John, John Smith"
- "S-M-I-T-H, first name John" (spellback)

Silently strip intro phrases like "my name is", "this is", "it's", "I am".
De-hyphenate spellback ("S-M-I-T-H" → "SMITH")."""


_NAME_TEXT_MODALITY = """\
Handle input as typed text.  Strip whitespace + trailing punctuation."""


def build_name_capture_prompt(
    modality: Modality = "audio",
    extra_instructions: str = "",
    on_enter_persona_hint: str = "",
) -> SlotCapturePrompt:
    """Build the name-slot sub-agent prompt."""
    modality_block = (
        _NAME_AUDIO_MODALITY if modality == "audio"
        else _NAME_TEXT_MODALITY
    )
    extra = f"\n{extra_instructions.strip()}" if extra_instructions else ""
    instructions = _NAME_BASE_INSTRUCTIONS.format(
        modality_specific=modality_block,
        extra_instructions=extra,
    )
    on_enter = (
        on_enter_persona_hint.strip()
        or "Ask the caller what name to put the booking under, briefly and warmly."
    )
    return SlotCapturePrompt(
        instructions=instructions,
        on_enter_prompt=on_enter,
        tools_hint=(
            "update_name", "decline_name_capture",
        ),
    )


# ── date-slot prompt ─────────────────────────────────────


_DATE_BASE_INSTRUCTIONS = """\
You are one narrow step in a broader conversation, responsible only for capturing a date for the booking.

{modality_specific}

Rules:
1. Call `update_date` at the FIRST hypothesis of a date.  Update again on every correction.
2. Never invent a date.  If the caller says something vague ('sometime next week'), ask them to narrow it down.
3. If the resolver flags the date as ambiguous ('this Tuesday' — this week or next?), read the two candidates back and ask which.
4. Never repeat the date back as raw ISO — use natural language ("Tuesday, September 2nd").
5. Ignore off-topic input.  Steer back: "Got it — what day are you thinking?"
6. No markdown, no filler openings.
7. Always invoke the tool explicitly.
{extra_instructions}"""


_DATE_AUDIO_MODALITY = """\
Handle input as noisy voice transcription.  Callers give dates in many shapes:
- "January 15th"
- "the 15th of January"
- "tomorrow" / "next Tuesday" / "this Friday"
- "1/15" / "January fifteenth twenty twenty six"

Silently normalize:
- Spelled ordinals ("fifteenth" → "15th")
- Strip fillers ("on", "the", "let's say")
- Preserve relative references ("tomorrow") — the tool resolves them."""


_DATE_TEXT_MODALITY = """\
Handle input as typed text.  Accept ISO (YYYY-MM-DD) or natural language."""


def build_date_capture_prompt(
    modality: Modality = "audio",
    extra_instructions: str = "",
    on_enter_persona_hint: str = "",
) -> SlotCapturePrompt:
    """Build the date-slot sub-agent prompt."""
    modality_block = (
        _DATE_AUDIO_MODALITY if modality == "audio"
        else _DATE_TEXT_MODALITY
    )
    extra = f"\n{extra_instructions.strip()}" if extra_instructions else ""
    instructions = _DATE_BASE_INSTRUCTIONS.format(
        modality_specific=modality_block,
        extra_instructions=extra,
    )
    on_enter = (
        on_enter_persona_hint.strip()
        or "Ask the caller what day they'd like to come in, briefly and warmly."
    )
    return SlotCapturePrompt(
        instructions=instructions,
        on_enter_prompt=on_enter,
        tools_hint=(
            "update_date", "decline_date_capture",
        ),
    )


# ── yes/no-slot prompt ───────────────────────────────────


_YES_NO_BASE_INSTRUCTIONS = """\
You are one narrow step in a broader conversation, responsible only for capturing a yes-or-no confirmation from the caller.

The caller has been told what they're confirming — do NOT restate the whole booking.  Just call `update_yes_no` with 'yes' or 'no' as soon as you hear their answer.

Rules:
1. Call `update_yes_no("yes")` for clear affirmatives: yes, yeah, yep, sure, absolutely, correct, that's right, sounds good, book it, do it, go ahead, please do.
2. Call `update_yes_no("no")` for clear negatives: no, nope, nah, cancel, wrong, that's not right, don't, hold on, wait.
3. For ambiguous answers ("maybe", "kind of", "I think so", "let me think"), do NOT call the tool — re-ask directly: "Just to confirm — yes or no?"
4. Ignore off-topic input.  Repeat the yes-or-no question directly.
5. No markdown, no filler.  Answer with the tool call, not with words.
{extra_instructions}"""


def build_yes_no_capture_prompt(
    modality: Modality = "audio",
    extra_instructions: str = "",
    on_enter_persona_hint: str = "",
) -> SlotCapturePrompt:
    """Build the yes/no-slot sub-agent prompt.  No modality split
    because yes/no reads identically in audio and text."""
    del modality  # not used — same instructions either way
    extra = f"\n{extra_instructions.strip()}" if extra_instructions else ""
    instructions = _YES_NO_BASE_INSTRUCTIONS.format(
        extra_instructions=extra,
    )
    on_enter = on_enter_persona_hint.strip() or ""
    return SlotCapturePrompt(
        instructions=instructions,
        on_enter_prompt=on_enter,
        tools_hint=("update_yes_no",),
    )


__all__ = [
    "Modality",
    "SlotCapturePrompt",
    "build_phone_capture_prompt",
    "build_email_capture_prompt",
    "build_name_capture_prompt",
    "build_date_capture_prompt",
    "build_yes_no_capture_prompt",
    "read_back_in_groups",
]

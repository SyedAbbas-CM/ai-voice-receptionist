"""Task #142 tests: email + name + date + yes_no slot parsers.

Adapted from LK's beta/workflows/{email_address,name,dob}.py.
Registry + normalizer + validator + LK sub-agent prompt for each.
"""
from __future__ import annotations

import pytest

from packages.slot_parsers.session import SlotStatus


# ── email ─────────────────────────────────────────────────


def test_email_normalize_spoken_dot_at():
    from packages.slot_parsers.email_slot import normalize_email
    assert normalize_email(
        "john dot smith at gmail dot com"
    ) == "john.smith@gmail.com"


def test_email_normalize_with_intro_phrase():
    from packages.slot_parsers.email_slot import normalize_email
    assert normalize_email(
        "my email is jane at yahoo dot com"
    ) == "jane@yahoo.com"


def test_email_normalize_spelled_hyphen_underscore():
    from packages.slot_parsers.email_slot import normalize_email
    assert normalize_email(
        "j hyphen smith underscore x at foo dot com"
    ) == "j-smith_x@foo.com"


def test_email_normalize_preserves_verbatim_at_symbol():
    from packages.slot_parsers.email_slot import normalize_email
    assert normalize_email("john@gmail.com") == "john@gmail.com"


def test_email_valid_regex_passes():
    from packages.slot_parsers.email_slot import email_validator
    r = email_validator("john.smith@gmail.com", {})
    assert r.status == SlotStatus.VALID
    assert r.value == "john.smith@gmail.com"


def test_email_typo_gmial_suggests_gmail():
    from packages.slot_parsers.email_slot import email_validator
    r = email_validator("john@gmial.com", {})
    assert r.status == SlotStatus.POSSIBLE
    assert r.value == "john@gmail.com"


def test_email_typo_yahoo_suggests_correction():
    from packages.slot_parsers.email_slot import email_validator
    r = email_validator("jane@yaho.com", {})
    assert r.status == SlotStatus.POSSIBLE
    assert r.value == "jane@yahoo.com"


def test_email_no_at_incomplete():
    from packages.slot_parsers.email_slot import email_validator
    r = email_validator("john", {})
    assert r.status == SlotStatus.INCOMPLETE


def test_email_only_at_no_domain_incomplete():
    from packages.slot_parsers.email_slot import email_validator
    r = email_validator("john@", {})
    assert r.status == SlotStatus.INCOMPLETE


def test_email_gibberish_invalid():
    """Email-shaped-but-not-valid input → INVALID.  Note: raw
    punctuation-only strings normalize to empty → INCOMPLETE not
    INVALID; INVALID fires when there IS structure but it doesn't
    match the email grammar."""
    from packages.slot_parsers.email_slot import email_validator
    r = email_validator("not@valid@email", {})
    assert r.status == SlotStatus.INVALID


def test_email_empty_incomplete():
    from packages.slot_parsers.email_slot import email_validator
    r = email_validator("", {})
    assert r.status == SlotStatus.INCOMPLETE


# ── name ──────────────────────────────────────────────


def test_name_strips_my_name_is():
    from packages.slot_parsers.name_slot import normalize_name
    assert normalize_name("my name is John Smith") == "John Smith"
    assert normalize_name("This is Jane Doe") == "Jane Doe"


def test_name_dehyphenates_spellback():
    from packages.slot_parsers.name_slot import normalize_name
    assert normalize_name("S-M-I-T-H") == "SMITH"


def test_name_valid_multi_word():
    from packages.slot_parsers.name_slot import name_validator
    r = name_validator("John Smith", {})
    assert r.status == SlotStatus.VALID
    assert r.value == "John Smith"


def test_name_single_word_possible():
    from packages.slot_parsers.name_slot import name_validator
    r = name_validator("John", {})
    assert r.status == SlotStatus.POSSIBLE
    assert r.value == "John"


def test_name_junk_null_invalid():
    from packages.slot_parsers.name_slot import name_validator
    r = name_validator("null", {})
    assert r.status == SlotStatus.INVALID


def test_name_junk_the_caller_invalid():
    from packages.slot_parsers.name_slot import name_validator
    r = name_validator("the caller", {})
    assert r.status == SlotStatus.INVALID


def test_name_all_digits_invalid():
    from packages.slot_parsers.name_slot import name_validator
    r = name_validator("12345", {})
    assert r.status == SlotStatus.INVALID


def test_name_single_char_incomplete():
    from packages.slot_parsers.name_slot import name_validator
    r = name_validator("J", {})
    assert r.status == SlotStatus.INCOMPLETE


def test_name_clean_arg_strips_quotes():
    from packages.slot_parsers.name_slot import _clean_name_arg
    assert _clean_name_arg("'John Smith'") == "John Smith"
    assert _clean_name_arg('"none"') == ""


def test_name_apostrophe_preserved():
    """O'Brien, Smith-Jones etc must survive."""
    from packages.slot_parsers.name_slot import name_validator
    r = name_validator("Mary O'Brien", {})
    assert r.status == SlotStatus.VALID
    assert "O'Brien" in r.value


# ── date ──────────────────────────────────────────────


def test_date_normalize_spelled_ordinal():
    from packages.slot_parsers.date_slot import normalize_date
    assert "15th" in normalize_date("January fifteenth")


def test_date_normalize_strips_on_prefix():
    from packages.slot_parsers.date_slot import normalize_date
    assert normalize_date("on Tuesday").startswith("tuesday")


def test_date_two_digit_year_expand_current_century():
    from packages.slot_parsers.date_slot import _expand_two_digit_year
    # 2026 baseline; '06' → 2006 (within +20)
    assert _expand_two_digit_year("06", 2026) == "2006"
    # '46' → 2046 (edge of +20 window)
    assert _expand_two_digit_year("46", 2026) == "2046"
    # '99' → 1999 (past 80 years)
    assert _expand_two_digit_year("99", 2026) == "1999"


def test_date_two_digit_year_ignores_four_digit():
    from packages.slot_parsers.date_slot import _expand_two_digit_year
    assert _expand_two_digit_year("2026", 2026) == "2026"


def test_date_two_digit_year_garbage_passes_through():
    from packages.slot_parsers.date_slot import _expand_two_digit_year
    assert _expand_two_digit_year("bogus", 2026) == "bogus"


def test_date_valid_resolvable():
    from packages.slot_parsers.date_slot import date_validator
    # TemporalResolver is designed for spoken/relative language,
    # not bare ISO.  Use its shape.
    r = date_validator("tomorrow", {"timezone": "UTC"})
    # Might be VALID or POSSIBLE depending on resolver — we just
    # verify no exception and non-INVALID.
    assert r.status in (SlotStatus.VALID, SlotStatus.POSSIBLE)


def test_date_empty_incomplete():
    from packages.slot_parsers.date_slot import date_validator
    r = date_validator("", {"timezone": "UTC"})
    assert r.status == SlotStatus.INCOMPLETE


def test_date_gibberish_invalid():
    from packages.slot_parsers.date_slot import date_validator
    r = date_validator("purple submarine", {"timezone": "UTC"})
    # Resolver returns UNRECOGNIZED → INVALID.
    assert r.status == SlotStatus.INVALID


# ── yes_no ───────────────────────────────────────────


@pytest.mark.parametrize("utterance", [
    "yes", "yeah", "yep", "yup", "sure", "absolutely",
    "correct", "that's right", "go ahead", "sounds good",
    "book it", "do it", "please", "ok", "okay",
])
def test_yes_no_yes_variants_all_map_to_yes(utterance):
    from packages.slot_parsers.yes_no_slot import yes_no_validator
    r = yes_no_validator(utterance, {})
    assert r.status == SlotStatus.VALID
    assert r.value == "yes"


@pytest.mark.parametrize("utterance", [
    "no", "nope", "nah", "no thanks", "cancel",
    "wrong", "that's not right", "negative", "don't",
])
def test_yes_no_no_variants_all_map_to_no(utterance):
    from packages.slot_parsers.yes_no_slot import yes_no_validator
    r = yes_no_validator(utterance, {})
    assert r.status == SlotStatus.VALID
    assert r.value == "no"


@pytest.mark.parametrize("utterance", [
    "maybe", "kind of", "kinda",
    "i don't know", "let me think",
])
def test_yes_no_ambiguous_returns_possible(utterance):
    from packages.slot_parsers.yes_no_slot import yes_no_validator
    r = yes_no_validator(utterance, {})
    assert r.status == SlotStatus.POSSIBLE
    assert r.value is None


def test_yes_no_yes_with_trailing_words():
    from packages.slot_parsers.yes_no_slot import yes_no_validator
    r = yes_no_validator("yes go ahead and book it", {})
    assert r.status == SlotStatus.VALID
    assert r.value == "yes"


def test_yes_no_no_with_context_wins_over_yes_substring():
    """'no that's not right' should NOT misfire as yes via 'right'."""
    from packages.slot_parsers.yes_no_slot import yes_no_validator
    r = yes_no_validator("no that's not right", {})
    assert r.status == SlotStatus.VALID
    assert r.value == "no"


def test_yes_no_empty_incomplete():
    from packages.slot_parsers.yes_no_slot import yes_no_validator
    r = yes_no_validator("", {})
    assert r.status == SlotStatus.INCOMPLETE


def test_yes_no_off_topic_invalid():
    from packages.slot_parsers.yes_no_slot import yes_no_validator
    r = yes_no_validator("what are your hours?", {})
    assert r.status == SlotStatus.INVALID


# ── registry ─────────────────────────────────────────


def test_registry_includes_all_four_new_kinds():
    from packages.slot_parsers.registry import list_slot_types
    kinds = list_slot_types()
    assert "email" in kinds
    assert "name" in kinds
    assert "date" in kinds
    assert "yes_no" in kinds
    # Phone still registered.
    assert "phone" in kinds


def test_registry_get_email_handlers():
    from packages.slot_parsers.registry import get_slot_handlers
    normalizer, validator = get_slot_handlers("email")
    assert normalizer is not None
    assert validator is not None
    # Validator round-trips.
    r = validator("john@gmail.com", {})
    assert r.status == SlotStatus.VALID


def test_registry_get_name_handlers():
    from packages.slot_parsers.registry import get_slot_handlers
    normalizer, validator = get_slot_handlers("name")
    assert normalizer is not None
    r = validator("Abbas Test", {})
    assert r.status == SlotStatus.VALID


def test_registry_get_date_handlers():
    from packages.slot_parsers.registry import get_slot_handlers
    normalizer, validator = get_slot_handlers("date")
    assert normalizer is not None
    assert validator is not None


def test_registry_get_yes_no_handlers():
    from packages.slot_parsers.registry import get_slot_handlers
    normalizer, validator = get_slot_handlers("yes_no")
    assert normalizer is not None
    r = validator("yeah", {})
    assert r.status == SlotStatus.VALID
    assert r.value == "yes"


# ── LK sub-agent prompts ────────────────────────────


def test_email_prompt_names_the_tool():
    from packages.slot_parsers.slot_capture_prompts import (
        build_email_capture_prompt,
    )
    p = build_email_capture_prompt()
    assert "update_email" in p.instructions
    assert "update_email" in p.tools_hint


def test_email_prompt_mentions_typo_suggestion_behavior():
    from packages.slot_parsers.slot_capture_prompts import (
        build_email_capture_prompt,
    )
    p = build_email_capture_prompt()
    lower = p.instructions.lower()
    assert "gmial" in lower or "typo" in lower or "suggest" in lower


def test_name_prompt_forbids_llm_junk():
    from packages.slot_parsers.slot_capture_prompts import (
        build_name_capture_prompt,
    )
    p = build_name_capture_prompt()
    assert "null" in p.instructions.lower()
    assert "the caller" in p.instructions.lower() or (
        "generic" in p.instructions.lower()
    )


def test_date_prompt_mentions_ambiguity_handling():
    from packages.slot_parsers.slot_capture_prompts import (
        build_date_capture_prompt,
    )
    p = build_date_capture_prompt()
    assert "ambiguous" in p.instructions.lower() or (
        "narrow" in p.instructions.lower()
    )


def test_yes_no_prompt_lists_both_variant_sets():
    from packages.slot_parsers.slot_capture_prompts import (
        build_yes_no_capture_prompt,
    )
    p = build_yes_no_capture_prompt()
    lower = p.instructions.lower()
    assert "yes" in lower
    assert "no" in lower
    assert "update_yes_no" in p.tools_hint[0]


def test_yes_no_prompt_covers_ambiguous_reask():
    from packages.slot_parsers.slot_capture_prompts import (
        build_yes_no_capture_prompt,
    )
    p = build_yes_no_capture_prompt()
    assert "maybe" in p.instructions.lower() or (
        "ambiguous" in p.instructions.lower()
    )

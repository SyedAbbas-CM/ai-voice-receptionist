"""Slot-capture LLM prompt discipline tests (LiveKit steal #7).

2026-08-29: sub-agent phone capture — adapted from LK phone_number.py.

These tests lock:
  * The rendered prompt contains the core rules a small model needs
    to not do Christiaan-class empty-completions.
  * Modality selection picks the right normalization block.
  * require_confirmation flips the confirm-tool inclusion.
  * read_back_in_groups produces the right groupings for common
    E.164 shapes including the exact Christiaan Dutch mobile shape.
"""
from __future__ import annotations

import pytest

from packages.slot_parsers.slot_capture_prompts import (
    build_phone_capture_prompt,
    read_back_in_groups,
)


# ── rendered prompt content ─────────────────────────────────


def test_audio_prompt_names_the_tool():
    p = build_phone_capture_prompt(modality="audio")
    assert "update_phone_number" in p.instructions
    assert "confirm_phone_number" in p.instructions


def test_audio_prompt_includes_dutch_example():
    """We got burned by Dutch numbers — the prompt SHOULD show a
    Dutch spoken pattern so the model doesn't drop into silent
    confusion the way BUG-CHR-01 did."""
    p = build_phone_capture_prompt(modality="audio")
    lower = p.instructions.lower()
    assert "dutch" in lower
    assert "nul" in lower  # Dutch for 'zero'


def test_audio_prompt_forbids_solid_block_readback():
    p = build_phone_capture_prompt(modality="audio")
    assert "read it in groups" in p.instructions.lower() or (
        "in groups" in p.instructions.lower()
    )


def test_audio_prompt_forbids_invention():
    p = build_phone_capture_prompt(modality="audio")
    assert "invent" in p.instructions.lower()


def test_audio_prompt_forbids_simulation():
    p = build_phone_capture_prompt(modality="audio")
    assert "simulate" in p.instructions.lower()


def test_text_modality_omits_spoken_variants():
    """Text modality block should NOT talk about spoken 'five five
    five' — that's audio-only."""
    p = build_phone_capture_prompt(modality="text")
    lower = p.instructions.lower()
    assert "typed text" in lower
    assert "spoken digits" not in lower


def test_extra_instructions_appended():
    p = build_phone_capture_prompt(
        modality="audio",
        extra_instructions="Prefer mobile numbers for this clinic.",
    )
    assert "Prefer mobile numbers" in p.instructions


def test_on_enter_default():
    p = build_phone_capture_prompt(modality="audio")
    assert "phone number" in p.on_enter_prompt.lower()


def test_on_enter_persona_override():
    p = build_phone_capture_prompt(
        modality="audio",
        on_enter_persona_hint=(
            "Say 'Grab your number real quick?' in an easy tone."
        ),
    )
    assert "grab your number" in p.on_enter_prompt.lower()


# ── confirmation gate flips tools ────────────────────────────


def test_require_confirmation_true_lists_confirm_tool():
    p = build_phone_capture_prompt(require_confirmation=True)
    assert "confirm_phone_number" in p.tools_hint


def test_require_confirmation_false_omits_confirm_tool():
    p = build_phone_capture_prompt(require_confirmation=False)
    assert "confirm_phone_number" not in p.tools_hint
    # But update + decline are still present.
    assert "update_phone_number" in p.tools_hint
    assert "decline_phone_number_capture" in p.tools_hint


def test_require_confirmation_false_changes_confirmation_block():
    p = build_phone_capture_prompt(require_confirmation=False)
    assert "do NOT need to ask" in p.instructions or (
        "resumes" in p.instructions
    )


# ── read_back_in_groups shape correctness ─────────────────


def test_readback_us_10_digit_shape():
    """5551234567 → 555, 123, 4567 — the shape a human receptionist
    speaks."""
    assert read_back_in_groups("5551234567") == "555, 123, 4567"


def test_readback_us_11_digit_with_leading_one():
    """15551234567 → 1, 555, 123, 4567."""
    assert read_back_in_groups("15551234567") == "1, 555, 123, 4567"


def test_readback_dutch_mobile_10_digit_starting_zero():
    """0625007600 (Christiaan's exact number) →
    06, 25, 00, 76, 00 — Dutch cellular convention."""
    assert read_back_in_groups("0625007600") == "06, 25, 00, 76, 00"


def test_readback_e164_us_number():
    """+15551234567 keeps the country code visible for read-back."""
    result = read_back_in_groups("+15551234567")
    # Should show the +1 prefix, then the standard NANP grouping.
    assert result.startswith("+1")
    assert "555" in result and "123" in result and "4567" in result


def test_readback_e164_dutch_number():
    """+31625007600 — Dutch international format.
    Country code visible + 2-digit body groupings."""
    result = read_back_in_groups("+31625007600")
    assert result.startswith("+3")
    # 2-digit groups after prefix.
    assert result.count(",") >= 3


def test_readback_empty_returns_empty():
    assert read_back_in_groups("") == ""


def test_readback_none_never_raises():
    # dataclass typing is str but real inputs may be dirty.
    assert read_back_in_groups("   ") == ""


def test_readback_custom_group_sizes():
    """Explicit override wins over auto-detection."""
    result = read_back_in_groups("1234567890", group_sizes=[4, 3, 3])
    assert result == "1234, 567, 890"


def test_readback_stripping_non_digits():
    """(555) 123-4567 → same as bare 5551234567."""
    assert read_back_in_groups("(555) 123-4567") == "555, 123, 4567"


def test_readback_fallback_2_digit_groups_for_unknown_shape():
    """A 9-digit non-standard shape gets 2-digit chunks."""
    result = read_back_in_groups("123456789")
    parts = [p.strip() for p in result.split(",")]
    # Should be mostly 2-digit chunks.
    assert all(len(p) <= 3 for p in parts)


# ── prompt is stable across invocations ─────────────────


def test_prompt_deterministic_same_inputs():
    """Same inputs → same instructions text.  If a small model
    encounters a version drift mid-call it drops context."""
    a = build_phone_capture_prompt(modality="audio")
    b = build_phone_capture_prompt(modality="audio")
    assert a.instructions == b.instructions
    assert a.tools_hint == b.tools_hint


def test_prompt_dataclass_is_frozen():
    p = build_phone_capture_prompt()
    with pytest.raises(Exception):
        p.instructions = "attempt mutation"  # type: ignore[misc]

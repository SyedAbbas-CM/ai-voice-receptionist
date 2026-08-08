"""2026-08-07: capability-gate tests.

Verifies MODEL_CAPABILITIES metadata is respected — a tool-less model
never gets a request with tools=[...] attached.
"""
from app.providers.llm.router_llm import (
    MODEL_CAPABILITIES,
    _model_supports,
    _PROVIDER_ALTERNATES,
)


def test_allam_2_7b_marked_no_tools():
    """allam-2-7b MUST be marked as no tool_calling — this is a
    correctness invariant, not a soft preference.  Groq's model page
    confirms zero tool support."""
    assert _model_supports("allam-2-7b", "tool_calling") is False
    assert _model_supports("allam-2-7b", "json_mode") is False


def test_unknown_model_supports_everything_by_default():
    """Models not listed in MODEL_CAPABILITIES are assumed capable."""
    assert _model_supports("some-random-brand-new-model", "tool_calling") is True
    assert _model_supports("some-random-brand-new-model", "json_mode") is True
    assert _model_supports("some-random-brand-new-model", "chat") is True


def test_whisper_marked_no_chat():
    """Whisper models are STT, not chat.  Should never be routed to."""
    assert _model_supports("whisper-large-v3", "chat") is False
    assert _model_supports("whisper-large-v3-turbo", "chat") is False


def test_allam_is_last_in_groq_alternates():
    """allam-2-7b should be the LAST groq alternate (last-resort only)
    because we can't tool-call with it."""
    groq_alts = _PROVIDER_ALTERNATES["groq"]
    assert "allam-2-7b" in groq_alts
    assert groq_alts[-1] == "allam-2-7b", (
        f"allam-2-7b should be LAST resort, got position "
        f"{groq_alts.index('allam-2-7b')}/{len(groq_alts)-1}"
    )

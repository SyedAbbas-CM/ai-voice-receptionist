"""Sprint 9d: VPL compiler tests.

Coverage per compiler (ElevenLabs, Cartesia):
  * Basic utterance -> valid payload shape
  * Style + intensity translate to provider-specific fields
  * Unsupported fields listed in `unsupported_fields`
  * Approximated fields listed in `approximations`
  * Restricted speech-acts (emergency/payment) produce compilable
    payloads that respect the safety envelope
  * Registry: get_compiler dispatches correctly

The compilers are pure functions (no network I/O), so these are unit
tests without any provider stubs.
"""
from __future__ import annotations

import pytest

from packages.voice.vpl import (
    Delivery,
    DeliveryStyle,
    Emphasis,
    Interruptibility,
    SafetyPolicy,
    SpeechAct,
    VPLUtterance,
)
from packages.voice.vpl.schema import VPLContext
from packages.voice.vpl.compilers import (
    compile_cartesia,
    compile_elevenlabs,
    get_compiler,
    register_compiler,
)


# ── ElevenLabs compiler ─────────────────────────────────────────────

def test_elevenlabs_basic_payload_shape():
    u = VPLUtterance(text="How can I help you today?")
    plan = compile_elevenlabs(u, voice_id="V123")
    assert plan.provider == "elevenlabs"
    assert plan.model == "eleven_turbo_v2_5"
    assert plan.output_format == "ulaw_8000"
    p = plan.request_payload
    assert p["text"] == "How can I help you today?"
    assert p["model_id"] == "eleven_turbo_v2_5"
    vs = p["voice_settings"]
    assert 0.0 <= vs["stability"] <= 1.0
    assert 0.0 <= vs["similarity_boost"] <= 1.0
    assert vs["use_speaker_boost"] is True


def test_elevenlabs_style_and_intensity_influence_style_field():
    u_neutral = VPLUtterance(text="hi")
    plan_neutral = compile_elevenlabs(u_neutral, voice_id="V")
    u_warm = VPLUtterance(
        text="hi",
        delivery=Delivery(style=DeliveryStyle.WARM, intensity=0.5),
    )
    plan_warm = compile_elevenlabs(u_warm, voice_id="V")
    assert plan_warm.request_payload["voice_settings"]["style"] > \
           plan_neutral.request_payload["voice_settings"]["style"]
    # Warm style should be flagged as approximation
    assert any("style" in a for a in plan_warm.approximations)


def test_elevenlabs_rate_clamped_to_turbo_range():
    u = VPLUtterance(text="hi", delivery=Delivery(rate=1.4))
    plan = compile_elevenlabs(u, voice_id="V")
    assert plan.request_payload["voice_settings"]["speed"] == 1.2
    assert any("rate" in a for a in plan.approximations)


def test_elevenlabs_pitch_reported_unsupported():
    u = VPLUtterance(text="hi", delivery=Delivery(pitch_semitones=3.0))
    plan = compile_elevenlabs(u, voice_id="V")
    assert any("pitch" in x for x in plan.unsupported_fields)


def test_elevenlabs_pauses_translate_to_punctuation():
    u = VPLUtterance(
        text="Hello",
        delivery=Delivery(pause_before_ms=500, pause_after_ms=200),
    )
    plan = compile_elevenlabs(u, voice_id="V")
    text = plan.request_payload["text"]
    assert text.startswith("...")   # 500ms -> ellipsis prefix
    assert text.endswith(", ") or text.endswith(",")  # 200ms -> comma suffix
    assert any("pause" in a for a in plan.approximations)


def test_elevenlabs_prior_spoken_text_included():
    u = VPLUtterance(
        text="Yes it is.",
        context=VPLContext(prior_spoken_text="Is Tuesday still open?"),
    )
    plan = compile_elevenlabs(u, voice_id="V")
    assert plan.request_payload["previous_text"] == "Is Tuesday still open?"


def test_elevenlabs_pronunciation_refs_included():
    u = VPLUtterance(
        text="Osteria Verde is booked for 7pm.",
        pronunciation_refs=["dict_osteria_verde"],
    )
    plan = compile_elevenlabs(u, voice_id="V")
    locators = plan.request_payload.get("pronunciation_dictionary_locators")
    assert locators == [{
        "pronunciation_dictionary_id": "dict_osteria_verde",
        "version_id": "latest",
    }]


def test_elevenlabs_breaths_unsupported_on_turbo():
    u = VPLUtterance(
        text="hi",
        delivery=Delivery(breaths="light"),
        safety=SafetyPolicy(allow_nonverbal_vocalisation=True),
    )
    plan = compile_elevenlabs(u, voice_id="V", model="eleven_turbo_v2_5")
    assert any("breath" in x for x in plan.unsupported_fields)


def test_elevenlabs_breaths_supported_on_v3():
    u = VPLUtterance(
        text="hi",
        delivery=Delivery(breaths="light"),
        safety=SafetyPolicy(allow_nonverbal_vocalisation=True),
    )
    plan = compile_elevenlabs(u, voice_id="V", model="eleven_v3")
    assert not any("breath" in x for x in plan.unsupported_fields)


# ── Cartesia compiler ───────────────────────────────────────────────

def test_cartesia_basic_payload_shape():
    u = VPLUtterance(text="Hello there.")
    plan = compile_cartesia(u, voice_id="C-abc")
    assert plan.provider == "cartesia"
    assert plan.model == "sonic-3"
    p = plan.request_payload
    assert p["transcript"] == "Hello there."
    assert p["voice"]["mode"] == "id"
    assert p["voice"]["id"] == "C-abc"
    assert p["language"] == "en"


def test_cartesia_output_format_ulaw():
    u = VPLUtterance(text="hi")
    plan = compile_cartesia(u, voice_id="C", output_format="ulaw_8000")
    fmt = plan.request_payload["output_format"]
    assert fmt == {"container": "raw", "encoding": "pcm_mulaw", "sample_rate": 8000}


def test_cartesia_style_maps_to_emotion_tokens():
    u = VPLUtterance(
        text="I'm so sorry.",
        speech_act=SpeechAct.APOLOGY,
        delivery=Delivery(style=DeliveryStyle.APOLOGETIC, intensity=0.4),
    )
    plan = compile_cartesia(u, voice_id="C")
    exp = plan.request_payload["voice"].get("__experimental_controls", {})
    assert "emotion" in exp
    assert any("sad" in tok for tok in exp["emotion"])
    assert any("style" in a for a in plan.approximations)


def test_cartesia_rate_maps_to_speed_enum():
    u_slow = VPLUtterance(text="hi", delivery=Delivery(rate=0.8))
    plan_slow = compile_cartesia(u_slow, voice_id="C")
    assert plan_slow.request_payload["voice"]["__experimental_controls"]["speed"] == "slow"

    u_fast = VPLUtterance(text="hi", delivery=Delivery(rate=1.3))
    plan_fast = compile_cartesia(u_fast, voice_id="C")
    assert plan_fast.request_payload["voice"]["__experimental_controls"]["speed"] == "fast"


def test_cartesia_pitch_unsupported():
    u = VPLUtterance(text="hi", delivery=Delivery(pitch_semitones=2.5))
    plan = compile_cartesia(u, voice_id="C")
    assert any("pitch" in x for x in plan.unsupported_fields)


def test_cartesia_stability_unsupported():
    u = VPLUtterance(text="hi", delivery=Delivery(stability=0.9))
    plan = compile_cartesia(u, voice_id="C")
    assert any("stability" in x for x in plan.unsupported_fields)


def test_cartesia_prior_spoken_text_included_as_metadata():
    u = VPLUtterance(
        text="Yes we do.",
        context=VPLContext(prior_spoken_text="Do you have parking?"),
    )
    plan = compile_cartesia(u, voice_id="C")
    assert plan.request_payload["_previous_transcript"] == "Do you have parking?"


def test_cartesia_locale_strips_region():
    u_us = VPLUtterance(text="hi", locale="en-US")
    plan_us = compile_cartesia(u_us, voice_id="C")
    assert plan_us.request_payload["language"] == "en"

    u_fr = VPLUtterance(text="bonjour", locale="fr-FR")
    plan_fr = compile_cartesia(u_fr, voice_id="C")
    assert plan_fr.request_payload["language"] == "fr"


# ── restricted speech acts through both compilers ───────────────────

@pytest.mark.parametrize("compile_fn", [compile_elevenlabs, compile_cartesia])
def test_emergency_utterance_compiles_cleanly(compile_fn):
    """An EMERGENCY utterance with policy-safe delivery should compile
    without either compiler raising."""
    u = VPLUtterance(
        text="Please stay on the line, I'm connecting you now.",
        speech_act=SpeechAct.EMERGENCY,
        delivery=Delivery(
            style=DeliveryStyle.URGENT,
            intensity=0.35,   # under emergency cap of 0.4
            rate=1.1,
            interruptibility=Interruptibility.LOW,
        ),
    )
    plan = compile_fn(u, voice_id="V")
    assert plan is not None


@pytest.mark.parametrize("compile_fn", [compile_elevenlabs, compile_cartesia])
def test_payment_utterance_compiles_cleanly(compile_fn):
    u = VPLUtterance(
        text="Please read me the sixteen digit number on your card.",
        speech_act=SpeechAct.PAYMENT,
        delivery=Delivery(intensity=0.2, rate=0.9),
    )
    plan = compile_fn(u, voice_id="V")
    assert plan is not None


# ── registry ────────────────────────────────────────────────────────

def test_get_compiler_returns_registered():
    assert get_compiler("elevenlabs") is compile_elevenlabs
    assert get_compiler("cartesia") is compile_cartesia


def test_get_compiler_unknown_provider_raises():
    with pytest.raises(KeyError, match="no VPL compiler"):
        get_compiler("nonexistent-provider")


def test_register_compiler_adds_to_registry():
    def dummy(u, *, voice_id):
        return None  # would return CompiledSpeechPlan in real use
    register_compiler("dummy-provider", dummy)
    assert get_compiler("dummy-provider") is dummy


# ── degradation report content ──────────────────────────────────────

def test_both_compilers_report_at_least_one_approximation_for_stylized_speech():
    """Warm style + non-neutral rate + pauses should produce at least
    one approximation entry on both providers."""
    u = VPLUtterance(
        text="Absolutely, let me check that for you.",
        speech_act=SpeechAct.ACKNOWLEDGE_THEN_TOOL,
        delivery=Delivery(
            style=DeliveryStyle.WARM,
            intensity=0.4,
            rate=0.95,
            pause_after_ms=200,
        ),
    )
    plan_11 = compile_elevenlabs(u, voice_id="V")
    plan_ct = compile_cartesia(u, voice_id="V")
    assert len(plan_11.approximations) >= 1
    assert len(plan_ct.approximations) >= 1


def test_compiled_plan_is_frozen():
    """CompiledSpeechPlan must be immutable so downstream consumers
    can trust what they got."""
    u = VPLUtterance(text="hi")
    plan = compile_elevenlabs(u, voice_id="V")
    with pytest.raises(Exception):  # pydantic ValidationError or TypeError
        plan.provider = "different"  # type: ignore[misc]

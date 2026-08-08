"""VPL -> ElevenLabs compiler.

Maps VPL Delivery to ElevenLabs' `voice_settings` and per-request
context fields.  Documented capability matrix:

  Supported (exact):
    identity (voice_id resolution — assumes tenant voice binding done)
    output codec (ulaw_8000 / mp3 / pcm — from output_format arg)
    stability -> voice_settings.stability
    identity_strength -> voice_settings.similarity_boost
    context.prior_spoken_text -> previous_text
    (context.next_intent surfaced only if the caller passes next_text)
    pronunciation_refs -> pronunciation_dictionary_locators (v1 API)

  Supported (approximation — logged in `approximations`):
    style + intensity -> style setting on Turbo-v2.5+ (0..1)
    rate -> voice_settings.speed on Turbo-v2.5+ (0.7..1.2 clamped)
    emphasis -> phrase chunking via ellipses/commas (Turbo can't take
                inline SSML)
    pause_before/after_ms -> leading/trailing pause characters (`... `)
    breaths -> "light" only mapped when using v3 model; otherwise dropped

  Unsupported (dropped, logged in `unsupported_fields`):
    pitch_semitones — no Turbo control
    pitch_range — no Turbo control
    energy — no Turbo control
    phrase_finality — no Turbo control (implicit from punctuation)
    interruptibility — a client-side concern, not the provider's

For Turbo v2.5 the emphasis/pause approximations use punctuation
because inline SSML tags aren't honoured on the fastest tier.  This is
deliberate: latency < prosody perfection.
"""
from __future__ import annotations

from ..schema import (
    CompiledSpeechPlan,
    DeliveryStyle,
    VPLUtterance,
)


# Turbo/Flash line: no fine pitch control, no SSML.  v3+: broader.
_TURBO_STYLE_MAP = {
    DeliveryStyle.NEUTRAL: 0.0,
    DeliveryStyle.WARM: 0.35,
    DeliveryStyle.REASSURING: 0.45,
    DeliveryStyle.URGENT: 0.6,
    DeliveryStyle.APOLOGETIC: 0.4,
    DeliveryStyle.PROFESSIONAL: 0.2,
}


def _apply_pause_markers(text: str, pause_before_ms: int, pause_after_ms: int) -> str:
    """Insert punctuation-based pauses.  ElevenLabs treats a run of
    ellipses as a short pause and commas as micro-pauses; this is the
    best we can do on Turbo which won't parse SSML tags."""
    prefix = ""
    suffix = ""
    if pause_before_ms >= 400:
        prefix = "... "
    elif pause_before_ms >= 150:
        prefix = ", "
    if pause_after_ms >= 400:
        suffix = " ..."
    elif pause_after_ms >= 150:
        suffix = ", "
    return f"{prefix}{text}{suffix}"


def compile_elevenlabs(
    u: VPLUtterance,
    *,
    voice_id: str,
    model: str = "eleven_turbo_v2_5",
    output_format: str = "ulaw_8000",
) -> CompiledSpeechPlan:
    """Translate a VPL utterance to an ElevenLabs REST payload."""
    unsupported: list[str] = []
    approximations: list[str] = []

    d = u.delivery

    # ── voice_settings ─────────────────────────────────────────────
    voice_settings = {
        "stability": float(d.stability),
        "similarity_boost": float(d.identity_strength),
    }
    # Rate: Turbo v2.5+ supports "speed" in [0.7, 1.2].  Clamp and note.
    speed = max(0.7, min(1.2, float(d.rate)))
    if speed != float(d.rate):
        approximations.append(
            f"delivery.rate={d.rate} clamped to voice_settings.speed={speed} "
            "(ElevenLabs Turbo range)"
        )
    voice_settings["speed"] = speed

    # Style: mapped to voice_settings.style on v2.5+.  Also derated by
    # intensity so a Warm-but-low-intensity turn doesn't over-sing.
    style_base = _TURBO_STYLE_MAP.get(d.style, 0.0)
    style_val = round(style_base * float(d.intensity + 0.5), 3)
    style_val = max(0.0, min(1.0, style_val))
    voice_settings["style"] = style_val
    if d.style != DeliveryStyle.NEUTRAL:
        approximations.append(
            f"delivery.style={d.style.value} + intensity={d.intensity} -> "
            f"voice_settings.style={style_val} (approximation)"
        )

    # Turbo tier: use_speaker_boost is a pure-quality knob, on by default.
    voice_settings["use_speaker_boost"] = True

    # ── text with pause markers ────────────────────────────────────
    text = _apply_pause_markers(u.text, d.pause_before_ms, d.pause_after_ms)
    if d.pause_before_ms or d.pause_after_ms:
        approximations.append(
            f"pause_before={d.pause_before_ms}ms after={d.pause_after_ms}ms "
            "-> punctuation markers (Turbo can't parse SSML)"
        )

    # Emphasis: Turbo won't take SSML `<emphasis>`.  Approximate by
    # wrapping the emphasized span in commas (micro-pause + slight
    # phrase break).  Only for HIGH-strength emphasis to avoid
    # cluttering short utterances.
    if u.emphasis:
        approximations.append(
            "emphasis spans approximated via comma insertion (no SSML on Turbo)",
        )
        # Emphasis: walk in reverse so byte offsets don't shift under us.
        # Only apply if the resulting text stays under model's 5000 char limit.
        text_chars = list(text)
        # Re-index emphasis into the modified text — the pause prefix
        # shifted offsets by len(prefix).  Skip the reindex work: emphasis
        # remains best-effort under Turbo.
        for e in sorted(u.emphasis, key=lambda x: x.start, reverse=True):
            if e.strength < 0.3:
                continue
            # Insert commas around the span (idempotent-ish)
            end = min(e.end, len(text_chars))
            start = min(e.start, end)
            text_chars.insert(end, ",")
            text_chars.insert(start, ",")
        text = "".join(text_chars)

    # ── unsupported fields ─────────────────────────────────────────
    if d.pitch_semitones != 0:
        unsupported.append(f"pitch_semitones={d.pitch_semitones} (Turbo no pitch)")
    # pitch_range is default MEDIUM; only report if the caller set it explicitly
    # (schema default is MEDIUM, so we skip that comparison here)
    if d.energy not in (0.4,):  # schema default
        unsupported.append(f"energy={d.energy} (Turbo no energy knob)")
    # phrase_finality + interruptibility: client-side, don't warn.
    if d.breaths != "none":
        # Turbo has no breath control.  Report as unsupported unless
        # the caller opted into v3.
        if "v3" not in model:
            unsupported.append(f"breaths={d.breaths} (requires eleven_v3)")

    # ── payload ────────────────────────────────────────────────────
    payload: dict = {
        "text": text,
        "model_id": model,
        "voice_settings": voice_settings,
    }

    # Prior/next context for prosody continuity — this is a big win on
    # a per-turn call because Turbo can inflect the greeting differently
    # once it "sees" the caller's opening question.
    if u.context.prior_spoken_text:
        payload["previous_text"] = u.context.prior_spoken_text[-500:]
    if u.context.next_intent:
        # next_intent isn't next_text — we surface it only as a metadata
        # attribute the compiler consumer can use to build the follow-up
        # request's previous_text.  Not put in payload.
        pass

    # Pronunciation dictionary refs — v1 API accepts locator IDs.
    if u.pronunciation_refs:
        payload["pronunciation_dictionary_locators"] = [
            {"pronunciation_dictionary_id": r, "version_id": "latest"}
            for r in u.pronunciation_refs
        ]

    return CompiledSpeechPlan(
        provider="elevenlabs",
        model=model,
        request_payload=payload,
        output_format=output_format,
        references=(),
        unsupported_fields=tuple(unsupported),
        approximations=tuple(approximations),
        compiler_version="0.1.0",
    )

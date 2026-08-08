"""VPL -> Cartesia compiler.

Cartesia's Sonic-3 SDK takes a `voice={mode: id, id: ...}` handle plus
optional `__experimental_controls` on newer models.  It also supports:

  * `language` — locale routing
  * `output_format` — {container, encoding, sample_rate}
  * SSE `context_id` — for turn-continuation prosody (Sprint 10)

Supported (exact):
  identity — voice id resolved from tenant binding
  output codec — pcm_s16le / ulaw_8000 selectable via output_format
  locale — mapped to `language` field
  context.prior_spoken_text — passed as `_previous_transcript` metadata
                              (SDK ignores unknown keys, so it's forward-
                              compat for when the model exposes it)

Supported (approximation — Sonic-3):
  rate -> __experimental_controls.speed
  style + intensity -> __experimental_controls.emotion (mapped table)
  pause_before/after_ms -> punctuation markers (SDK doesn't accept SSML)
  emphasis -> phrase chunking (same reason)

Unsupported (dropped, logged):
  stability / identity_strength (Cartesia doesn't expose)
  pitch_semitones / pitch_range (Sonic-3 no pitch knob)
  energy (no direct control)
  breaths (SDK has no breath insertion)
  phrase_finality / interruptibility (client-side)
"""
from __future__ import annotations

from ..schema import (
    CompiledSpeechPlan,
    DeliveryStyle,
    VPLUtterance,
)


# Cartesia __experimental_controls.emotion is a list of tokens like
# ["positivity:high", "curiosity"].  We derive per-style token bundles.
_STYLE_EMOTION_TOKENS = {
    DeliveryStyle.NEUTRAL: [],
    DeliveryStyle.WARM: ["positivity:high", "curiosity"],
    DeliveryStyle.REASSURING: ["positivity:medium"],
    DeliveryStyle.URGENT: ["surprise:high"],
    DeliveryStyle.APOLOGETIC: ["sadness:low"],
    DeliveryStyle.PROFESSIONAL: [],
}

_INTENSITY_SPEED_MAP = {
    # Cartesia speed enum: "slow" | "normal" | "fast" (Sonic-3).  We
    # bucket the numeric rate rather than passing a float.
    "slow": (0.6, 0.9),
    "normal": (0.9, 1.1),
    "fast": (1.1, 1.4),
}


def _rate_to_speed_enum(rate: float) -> str:
    for tag, (lo, hi) in _INTENSITY_SPEED_MAP.items():
        if lo <= rate < hi:
            return tag
    # Out of range -> normal (schema clamps rate to [0.6, 1.4] already)
    return "normal"


def _default_output_format(codec: str) -> dict:
    if codec == "ulaw_8000":
        return {"container": "raw", "encoding": "pcm_mulaw", "sample_rate": 8000}
    if codec == "pcm_16000":
        return {"container": "raw", "encoding": "pcm_s16le", "sample_rate": 16000}
    if codec == "pcm_24000":
        return {"container": "raw", "encoding": "pcm_s16le", "sample_rate": 24000}
    if codec == "mp3":
        return {"container": "mp3"}
    return {"container": "raw", "encoding": "pcm_s16le", "sample_rate": 16000}


def _apply_pause_markers(text: str, pause_before_ms: int, pause_after_ms: int) -> str:
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


def compile_cartesia(
    u: VPLUtterance,
    *,
    voice_id: str,
    model: str = "sonic-3",
    output_format: str = "pcm_16000",
) -> CompiledSpeechPlan:
    """Translate a VPL utterance to a Cartesia SSE request payload."""
    unsupported: list[str] = []
    approximations: list[str] = []

    d = u.delivery

    # ── experimental_controls ──────────────────────────────────────
    exp_controls: dict = {}
    if d.rate != 1.0:
        exp_controls["speed"] = _rate_to_speed_enum(d.rate)
        approximations.append(
            f"delivery.rate={d.rate} -> __experimental_controls.speed="
            f"{exp_controls['speed']!r} (enum bucket)"
        )
    if d.style != DeliveryStyle.NEUTRAL:
        emotions = list(_STYLE_EMOTION_TOKENS.get(d.style, []))
        if emotions:
            exp_controls["emotion"] = emotions
            approximations.append(
                f"delivery.style={d.style.value} + intensity={d.intensity} "
                f"-> emotion={emotions} (style-token bundle)"
            )

    # ── text with pause markers ────────────────────────────────────
    text = _apply_pause_markers(u.text, d.pause_before_ms, d.pause_after_ms)
    if d.pause_before_ms or d.pause_after_ms:
        approximations.append(
            f"pause_before={d.pause_before_ms}ms after={d.pause_after_ms}ms "
            "-> punctuation markers (SDK no SSML)"
        )
    if u.emphasis:
        approximations.append(
            "emphasis spans approximated via comma insertion (no SSML)"
        )

    # ── unsupported fields ─────────────────────────────────────────
    if d.stability != 0.5:
        unsupported.append(f"stability={d.stability} (Cartesia no exposure)")
    if d.identity_strength != 0.75:
        unsupported.append(
            f"identity_strength={d.identity_strength} (Cartesia no exposure)"
        )
    if d.pitch_semitones != 0:
        unsupported.append(
            f"pitch_semitones={d.pitch_semitones} (Sonic-3 no pitch knob)"
        )
    if d.energy != 0.4:
        unsupported.append(f"energy={d.energy} (Sonic-3 no energy knob)")
    if d.breaths != "none":
        unsupported.append(f"breaths={d.breaths} (SDK no breath insertion)")

    # ── payload ────────────────────────────────────────────────────
    fmt = _default_output_format(output_format)
    payload: dict = {
        "model_id": model,
        "transcript": text,
        "voice": {"mode": "id", "id": voice_id},
        "output_format": fmt,
        "language": u.locale.split("-")[0],
    }
    if exp_controls:
        payload["voice"]["__experimental_controls"] = exp_controls

    if u.pronunciation_refs:
        # Cartesia pronunciation dictionary IDs — surfaced as metadata
        # for the client to pass to the SDK's pronunciation endpoint
        # (not a direct request field on the current SDK).
        payload["_pronunciation_refs"] = list(u.pronunciation_refs)
        approximations.append(
            "pronunciation_refs surfaced as metadata (SDK no direct field)"
        )

    if u.context.prior_spoken_text:
        # SDK ignores unknown top-level keys — forward-compat surface.
        payload["_previous_transcript"] = u.context.prior_spoken_text[-500:]

    return CompiledSpeechPlan(
        provider="cartesia",
        model=model,
        request_payload=payload,
        output_format=output_format,
        references=(),
        unsupported_fields=tuple(unsupported),
        approximations=tuple(approximations),
        compiler_version="0.1.0",
    )

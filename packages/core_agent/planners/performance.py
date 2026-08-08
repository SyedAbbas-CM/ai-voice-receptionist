"""Performance planner — the HOW of a turn.

Fast structured-output LLM call that produces a VPL Delivery block for
an agent utterance.  Default model: llama-3.1-8b-instant on Groq
(~150ms p50).

Design contract:
  * ALWAYS returns a PerformancePlan — no exceptions leak out.
  * On timeout / LLM error / malformed JSON → deterministic fallback
    from packages.voice.vpl.defaults.default_delivery_for(speech_act).
  * `used_fallback=True` on any degraded path; caller reads this to
    bump the two_planner_hit metric.

Kept intentionally small and injection-friendly.  The `llm` argument
is an LLMProvider (same interface as the semantic brain), so tests
inject a stub and prod injects the router or a dedicated Groq client.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Optional

from packages.voice.vpl import (
    Delivery,
    DeliveryStyle,
    SafetyPolicy,
    SpeechAct,
    VPLUtterance,
    default_delivery_for,
)
from packages.voice.vpl.validator import validate_vpl_and_repair


# Sprint 10 D2 (2026-08-04): critical spans identifier — regex + heuristic
# pass over agent reply text to surface phrase-level emphasis targets
# (dates, times, names, phone numbers, prices).  Fed to the perf planner
# so it can request emphasis on the parts that must land clearly.
_CRITICAL_PATTERNS = [
    # ISO date + time
    ("datetime", r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?\b"),
    # ISO date
    ("date", r"\b\d{4}-\d{2}-\d{2}\b"),
    # US phone in +1 or dashed form
    ("phone", r"(?:\+\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b"),
    # Money
    ("money", r"\$\d{1,4}(?:[.,]\d{2})?"),
    # Named times ("10:30 AM", "3 pm")
    ("time", r"\b\d{1,2}(?::\d{2})?\s*(?:AM|PM|am|pm|a\.m\.|p\.m\.)\b"),
    # Day of week + optional date
    ("weekday",
     r"\b(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
     r"(?:,?\s+(?:January|February|March|April|May|June|July|August|"
     r"September|October|November|December)\s+\d{1,2}(?:st|nd|rd|th)?)?\b"),
]


import re as _re
_CRITICAL_REGEXES = [(kind, _re.compile(pat)) for kind, pat in _CRITICAL_PATTERNS]


def extract_critical_spans(text: str) -> list[dict]:
    """Find dates/times/phones/prices/weekdays in an agent utterance.
    Returned as list of {kind, text, start, end} dicts, non-overlapping,
    earliest-first.  Fed to the perf planner so it can request emphasis."""
    spans: list[dict] = []
    seen_ranges: list[tuple[int, int]] = []
    for kind, regex in _CRITICAL_REGEXES:
        for m in regex.finditer(text):
            s, e = m.span()
            # Skip if overlaps a previously-matched span
            if any(s < pe and e > ps for ps, pe in seen_ranges):
                continue
            spans.append({"kind": kind, "text": m.group(0), "start": s, "end": e})
            seen_ranges.append((s, e))
    spans.sort(key=lambda x: x["start"])
    return spans

log = logging.getLogger(__name__)


PERFORMANCE_PROMPT = """You are a voice delivery planner. Given text an AI \
receptionist is about to say and its speech_act tag, return a JSON object \
describing HOW it should be delivered.

speech_act = {speech_act}
business_name = {business_name}
text = {text!r}
{caller_state_block}{critical_spans_block}

Return ONLY this JSON, no prose:
{{
  "style": "neutral|warm|reassuring|urgent|apologetic|professional",
  "intensity": 0.0 to 1.0,
  "rate": 0.6 to 1.4,
  "pause_before_ms": 0 to 1500,
  "pause_after_ms": 0 to 1500
}}

Guidelines:
- greeting: warm, rate 0.95, small pause after
- deliver_bad_news: reassuring, rate 0.9, low intensity, small pause before
- emergency: urgent but low intensity, faster rate
- payment: professional, calm, low intensity
- confirm: professional, slight pause after
- neutral: all defaults
- if the caller sounds frustrated or urgent: shorter/faster + concise
- if critical spans present (dates/times/names): pause_after_ms 200 so
  those pieces land clearly for the listener
"""


_JSON_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)


@dataclass(frozen=True)
class PerformancePlan:
    """What the performance planner returned for one utterance.

    Always constructible — no error paths leak past the planner.
    `used_fallback=True` means the LLM path failed for any reason and
    the delivery came from default_delivery_for(speech_act)."""
    delivery: Delivery
    used_fallback: bool
    latency_ms: int
    error: Optional[str] = None


class PerformancePlanner:
    """Small, fast LLM call that plans delivery per utterance.

    Usage:
        planner = PerformancePlanner(llm=groq_8b_client, timeout_ms=200)
        plan = await planner.plan(text, speech_act, business_name)
        # plan.delivery always usable — either LLM output or default
    """

    def __init__(
        self,
        llm,                              # LLMProvider (duck-typed)
        timeout_ms: int = 200,
        model: str = "llama-3.1-8b-instant",
        safety: Optional[SafetyPolicy] = None,
    ) -> None:
        self._llm = llm
        self._timeout_s = timeout_ms / 1000.0
        self._model = model
        self._safety = safety or SafetyPolicy()

    async def plan(
        self,
        text: str,
        speech_act: SpeechAct,
        business_name: str = "",
        *,
        caller_state: Optional[dict] = None,
        critical_spans: Optional[list[dict]] = None,
    ) -> PerformancePlan:
        """Sprint 10 D2 (2026-08-04): caller_state carries acoustic
        signals (frustration, urgency, speaking_rate) from
        AcousticTurnFeatures; critical_spans carries emphasis targets
        from extract_critical_spans().  Both optional; None-safe."""
        started = time.monotonic()

        def _fallback(reason: str, err: Optional[str] = None) -> PerformancePlan:
            latency_ms = int((time.monotonic() - started) * 1000)
            log.debug("perf planner fallback (%s): %s", reason, err)
            return PerformancePlan(
                delivery=default_delivery_for(speech_act),
                used_fallback=True,
                latency_ms=latency_ms,
                error=err,
            )

        # Auto-extract critical spans if caller didn't supply
        if critical_spans is None:
            critical_spans = extract_critical_spans(text)

        # Build the caller-state hint block (only if we have signal)
        caller_state_block = ""
        if caller_state:
            frustration = caller_state.get("frustration")
            urgency = caller_state.get("urgency")
            rate = caller_state.get("speaking_rate")
            bits = []
            if frustration is not None and frustration > 0.4:
                bits.append(f"frustration_high={frustration:.2f}")
            if urgency is not None and urgency > 0.4:
                bits.append(f"urgency_high={urgency:.2f}")
            if rate:
                bits.append(f"caller_rate={rate}")
            if bits:
                caller_state_block = f"\ncaller_state = {', '.join(bits)}"

        # Critical spans hint block
        critical_spans_block = ""
        if critical_spans:
            spans_repr = ", ".join(
                f"{s['kind']}={s['text']!r}" for s in critical_spans[:6]
            )
            critical_spans_block = f"\ncritical_spans = [{spans_repr}]"

        prompt = PERFORMANCE_PROMPT.format(
            speech_act=speech_act.value,
            business_name=business_name or "the business",
            text=text[:400],  # cap prompt size — long turns still get planned
            caller_state_block=caller_state_block,
            critical_spans_block=critical_spans_block,
        )
        messages = [{"role": "user", "content": prompt}]

        try:
            response = await asyncio.wait_for(
                self._llm.complete(
                    messages, tools=None,
                    temperature=0.2, max_tokens=200,
                ),
                timeout=self._timeout_s,
            )
        except asyncio.TimeoutError:
            return _fallback("timeout", f"exceeded {self._timeout_s*1000:.0f}ms")
        except Exception as e:
            return _fallback("llm-error", f"{type(e).__name__}: {e}")

        raw = (response.text or "").strip()
        if not raw:
            return _fallback("empty-response")

        # Parse JSON — tolerate lead/trail text via regex extraction
        parsed: Optional[dict] = None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            m = _JSON_RE.search(raw)
            if m:
                try:
                    parsed = json.loads(m.group(0))
                except json.JSONDecodeError as e:
                    return _fallback("bad-json", f"{e}")
            else:
                return _fallback("no-json-in-response", raw[:200])

        if not isinstance(parsed, dict):
            return _fallback("not-a-dict")

        try:
            delivery = _delivery_from_dict(parsed, speech_act)
        except Exception as e:
            return _fallback("invalid-delivery-fields", str(e))

        # Validate + repair against the safety envelope.  This catches
        # things like intensity=0.9 for an emergency (repair clamps).
        try:
            probe = VPLUtterance(
                text=text[:100] or "x",
                speech_act=speech_act,
                delivery=delivery,
                safety=self._safety,
            )
            probe, _ = validate_vpl_and_repair(probe)
            delivery = probe.delivery
        except Exception as e:
            return _fallback("validation", str(e))

        latency_ms = int((time.monotonic() - started) * 1000)
        return PerformancePlan(
            delivery=delivery,
            used_fallback=False,
            latency_ms=latency_ms,
        )


def _delivery_from_dict(d: dict, speech_act: SpeechAct) -> Delivery:
    """Build a Delivery from a loose dict.  Missing fields inherit from
    the speech-act default so we can accept partial JSON."""
    defaults = default_delivery_for(speech_act)
    style = d.get("style", defaults.style.value if isinstance(defaults.style, DeliveryStyle) else "neutral")
    if isinstance(style, str):
        try:
            style_enum = DeliveryStyle(style.lower())
        except ValueError:
            style_enum = defaults.style
    else:
        style_enum = defaults.style

    return Delivery(
        style=style_enum,
        intensity=float(d.get("intensity", defaults.intensity)),
        rate=float(d.get("rate", defaults.rate)),
        energy=float(d.get("energy", defaults.energy)),
        pitch_semitones=float(d.get("pitch_semitones", defaults.pitch_semitones)),
        pitch_range=defaults.pitch_range,           # not LLM-tunable in v0
        stability=float(d.get("stability", defaults.stability)),
        identity_strength=float(d.get("identity_strength", defaults.identity_strength)),
        phrase_finality=defaults.phrase_finality,
        interruptibility=defaults.interruptibility,
        pause_before_ms=int(d.get("pause_before_ms", defaults.pause_before_ms)),
        pause_after_ms=int(d.get("pause_after_ms", defaults.pause_after_ms)),
        breaths=defaults.breaths,                   # never LLM-tunable
    )

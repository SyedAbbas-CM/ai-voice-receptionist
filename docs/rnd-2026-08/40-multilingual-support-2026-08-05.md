# Multilingual Support Plan — Dutch / Hindi / Urdu

**Date parked:** 2026-08-05
**Status:** Not started. Master English first, then use this doc as the plan.
**Priority:** After the current bug-audit + intelligence-kernel work stabilizes.

## Summary

Dutch → Hindi → Urdu in order of feasibility. Dutch is production-ready with our
existing stack. Hindi is production-ready after Hinglish testing. Urdu needs a
separate provider route.

## Feasibility ranking (as of 2026-08-04)

| Language      | LLM intelligence                        | STT              | ElevenLabs TTS                                            | Status                                |
| ------------- | --------------------------------------- | ---------------- | --------------------------------------------------------- | ------------------------------------- |
| Dutch `nl-NL` | Excellent                               | Excellent        | Excellent — v3 + Multilingual v2 + Flash v2.5 + PVC       | Production-ready                      |
| Hindi `hi-IN` | Very strong (Claude Sonnet 4.5 ~96.7%)  | Strong           | Excellent — v3 + Multilingual v2 + Flash v2.5 + PVC       | Production-ready after Hinglish tests |
| Urdu `ur-PK`  | Good but less consistently benchmarked  | More variable    | Only Eleven v3 (no Flash v2.5, no PVC)                    | Viable with a separate Urdu stack     |

## LLM per-language notes

- **Claude** — Sonnet 4.5 scored ~96.7% of English performance on Anthropic's
  multilingual eval for Hindi. Recommends fixing response language in system
  prompt + using native script (not transliteration). Urdu/Dutch not separately
  benchmarked → we test ourselves.
- **Gemini Live** — explicitly lists Dutch, Hindi, and Urdu among 97 supported
  languages. Native-audio can switch languages mid-conversation. Strongest bet
  for Urdu-English and Hindi-English callers.
- **GPT / OpenAI** — generates all three fine; less-clear published matrix.
  Speech supports Dutch/Hindi/Urdu but built-in voices are English-optimized.

Router work needed: **language capability becomes part of the model-routing
contract**, not just a prompt hint.

## ElevenLabs TTS per-language

- **Dutch**: v3 + Multilingual v2 + Flash v2.5 + Professional Voice Cloning.
  Straightforward.
- **Hindi**: same full support. Fast cloned Hindi voice possible.
- **Urdu**: v3 + v3 Conversational only. NO Flash v2.5, NO Multilingual v2, NO PVC.
  Cloned voices in one language may retain accent or mispronounce in another —
  train Hindi clones on Hindi audio, Dutch clones on Dutch audio.

## STT per-language

- **Dutch**: ElevenLabs Scribe best group (≤5% WER). Deepgram Nova-3 Multilingual.
- **Hindi**: Scribe 5-10% WER. Deepgram realtime multilingual supports
  Hindi + language switching. **Hinglish is the main challenge** — need a mixed
  English-Hindi eval set (business names, phone numbers, Indian names).
- **Urdu**: Deepgram Nova-3 has dedicated Urdu (`language=ur`) but Urdu is
  NOT part of the ten-language `multi` code-switching set. ElevenLabs Scribe
  supports Urdu but places it in **25-50% WER** group — not good enough
  blindly. Needs a real Pakistani-telephone eval set.

## Other providers to consider

- **Gemini Live** — best experimental path for Urdu-English mixed calls.
- **Azure Speech** — has `ur-PK-AsadNeural` + `ur-PK-UzmaNeural` — serious Urdu
  fallback when we need explicit Pakistani locale (stock voices, not cloned).
- **Cartesia Sonic 3.5** — Hindi + Dutch (no Urdu). Useful for Dutch/Hindi
  low-latency benchmarks. Not for Urdu.

## Routing recommendations

### Dutch receptionist
```
Locale: nl-NL
LLM: Claude / Gemini / GPT
STT: Deepgram Nova-3 Multilingual
TTS: ElevenLabs Flash v2.5 cloned Dutch voice
Alt TTS: Cartesia Sonic 3.5
```

### Hindi receptionist
```
Locale: hi-IN
LLM: Claude / Gemini
STT: Deepgram Nova-3 Multilingual
TTS: ElevenLabs Flash v2.5 cloned Hindi voice
Alt TTS: Cartesia Sonic 3.5
Note: support Hindi-English code-switching explicitly, not as edge case
```

### Pakistani Urdu receptionist
```
Locale: ur-PK
LLM: Gemini or Claude (evaluate vs GPT)
STT primary: Deepgram Nova-3 language=ur
STT experiment: Gemini Live native audio
TTS primary experiment: Eleven v3 Conversational
TTS reliable fallback: Azure ur-PK voice
Native-audio experiment: Gemini Live
Do NOT route Urdu through the same ElevenLabs Flash path as Hindi/Dutch — Flash lacks Urdu
```

## Language-state design (add to session)

Not just `{language}` in the prompt. Real state:

```python
class LanguageState:
    primary_locale: str          # ur-PK, hi-IN, nl-NL, en-US
    current_language: str
    allowed_code_switches: list[str]
    output_script: str
    confidence: float
    explicitly_selected: bool
```

Policy per locale:

```yaml
ur-PK:
  script: Arabic
  permit_english_terms: true
  forbid_devanagari: true
  speech_provider: eleven_v3
  fallback_provider: azure_ur_pk

hi-IN:
  script: Devanagari
  permit_english_terms: true
  speech_provider: eleven_flash_v2_5

nl-NL:
  variant: Netherlands
  forbid_variant: nl-BE
  speech_provider: eleven_flash_v2_5
```

**For Urdu and Hindi, do NOT continuously auto-detect between them.** Spoken
forms overlap enough that auto-detection picks wrong script/locale. Let caller
choose at greeting or detect once + confirm when uncertain + lock the call's
primary language.

## Tool-data normalization (language-independent)

Normalize tool arguments separately from spoken language:

```
Caller says: "جمعرات چار بجے"
Canonical state: 2026-08-06T16:00:00+05:00
Spoken confirmation: "جمعرات، چھ اگست، شام چار بجے"
```

Prevents language changes from altering dates, phone numbers, booking arguments.
Our TemporalResolver already does this for English; extend it per locale.

## Build order

1. **Dutch** — proves the router + language-state design. Existing stack works.
2. **Hindi** — same architecture + Hinglish eval set. Tests code-switching hardening.
3. **Urdu** — deliberate experimental lane. Provider-specific branch. Pakistani
   telephone audio for STT eval. Stock `ur-PK` voice until better cloning support.

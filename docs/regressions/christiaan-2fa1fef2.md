# Regression: Christiaan silent-hangup

**CallSid:** `CA2fa1fef2065a7df388c3d6f58d7a7792`
**Session ID:** `twilio_CA2fa1fef2065a7df388c3d6f58d7a7792`
**Date:** 2026-08-27 14:27:34 → 14:28:38 UTC (~64s)
**Tenant:** `default` (pre-migration)
**Caller number:** `0625007600` (Dutch mobile, no country code spoken)
**Bug track:** CALL-BUG-03 (task #88)
**Fixes shipped:** commits `f443fd7` (BUG-CHR-01), `cd65a49` (BUG-CHR-02), `10d1283` (BUG-CHR-03), `07129b8` (task #97 ASK_SLOT wiring), `5335e8f` + `93c50f9` (humanness event emission), `8ebd441` (grammar-regex fix + BargeInPolicy)

## What broke

Real caller ("Christian" / Christiaan, Dutch). Two independent failures compounded:

1. **Service-alias miss (BUG-CHR-03):** Caller said "A follow-up" as the appointment type. The agent's alias resolver had no entry for "follow-up" → `Follow-up visit` service, so the LLM couldn't ground on any known service and returned canned "I'm sorry, could you say that again?" **4 times** on the exact same input.

2. **Phone-slot capture missing (BUG-CHR-02 + task #97):** Caller eventually said "My name is Christian, and my number is zero six two five zero zero seven six zero zero." The wide-scope brain couldn't parse the spoken-digits format, couldn't recognize the Dutch 10-digit as a valid E.164 candidate, produced an empty completion, and the agent said "I'm sorry, could you say that again?" one more time. Caller hung up.

Total dead-air-per-turn: ~8s on the failed number-capture turn. Total call: 64s of frustration ending in silent hangup.

## Full transcript (verbatim from DB)

```
[14:27:34] AGENT:   "Thanks for calling Smile Dental Clinic, how can I help?"
[14:27:48] CALLER:  "An exam, please."
[14:27:50] AGENT:   "Got it, you're looking for an exam, is this for a new patient, or a follow-up?"
[14:27:54] CALLER:  "An exam, please."
[14:27:56] AGENT:   "Sure thing! Just to clarify, is this a new patient exam, or a regular check-up?"
[14:27:56] CALLER:  "A follow-up."
[14:28:04] AGENT:   "I'm sorry, could you say that again?"        ← FAIL 1
[14:28:07] CALLER:  "A follow-up. I'm sorry."
[14:28:07] AGENT:   "I'm sorry, could you say that again?"        ← FAIL 2
[14:28:08] CALLER:  "a follow-up"
[14:28:09] AGENT:   "I'm sorry, could you say that again?"        ← FAIL 3
[14:28:11] CALLER:  "A follow-up. I'm sorry. A follow-up."
[14:28:12] AGENT:   "No problem at all! I can help with that, What's the best number to reach you at?"
[14:28:13] CALLER:  "A follow-up."
[14:28:14] AGENT:   "Got it, a follow-up exam, what's the best number to reach you at?"
[14:28:17] CALLER:  "For follow ups,"
[14:28:18] AGENT:   "I just need your name, and a good number to reach you at for the follow-up exam."
[14:28:29] CALLER:  "My name is Christian, and my number is zero six two five zero zero seven six"
[14:28:30] CALLER:  "My name is Christian, and my number is zero six two five zero zero seven six zero zero."
[14:28:38] AGENT:   "I'm sorry, could you say that again?"        ← FAIL 4 (phone capture)
                                                                    ← Christiaan hangs up
```

## Root cause per failure

| Failure | Root cause | File | Fix |
|---------|------------|------|-----|
| FAIL 1-3 | `resolve_service()` had no alias for "follow-up" | `packages/integrations/vertical_tools.py` | commit `10d1283` — added alias map |
| FAIL 3 (bonus) | `speech_sanitizer._TOOL_LEAK_PATTERNS` had `\bcheck[_ ]availability\b` stripping natural English from caller utterances, producing garbled inputs the LLM couldn't parse | `packages/voice/speech_sanitizer.py` | commit `8ebd441` — narrower regex + 17 regression tests |
| FAIL 4 | Wide-scope brain couldn't handle spoken digits ("zero six two five...") from a Dutch 10-digit; no fallback for empty LLM completions; no phone-slot capture invocation | `packages/core_agent/brain.py` + `apps/api/app/routes/twilio_actor.py` | commits `f443fd7` (empty-completion watchdog), `cd65a49` (libphonenumber int'l accept), `07129b8` (ASK_SLOT → actor.enter_slot_capture wire) |

## What today's deploy fixes

**Deploy timestamp:** 2026-08-29 13:18:02 UTC (commit `07129b8` and earlier)
**Feature flag:** `NEXT_ACTION_POLICY_ENABLED=true` (verified on box)

Now when a Christiaan-style caller says "A follow-up":
1. `service_resolution` event fires with `kind=match_exact`, `canonical_name="Follow-up visit"` — no more 4x "could you say that again"
2. `policy_decision(action=ask_slot, requested_slot=phone)` fires when phone is missing
3. Actor's `_on_policy_decision_callback` invokes `enter_slot_capture(kind="phone", accepted_regions=["NL","US"])`
4. `slot_capture_prompt_active` event fires — the brain uses the LK sub-agent narrow prompt during the number turn
5. Caller says "zero six two five zero zero seven six zero zero" → libphonenumber parses as `+31625007600` → validated E.164 stashed on `actor._last_validated_phone`
6. NO `empty_llm_completion` events, NO `empty_llm_deterministic_fallback` events

## How to reproduce (regression test)

**Live test:**
```bash
# Call the Twilio number, script:
1. "I want to book a follow-up"
2. When agent asks for phone: "zero six two five zero zero seven six zero zero"
# Then pull the trace:
./scripts/trace_call.sh CA<sid>
```

**Regression sweep from DB:**
```bash
./scripts/trace_call.sh CA2fa1fef2065a7df388c3d6f58d7a7792
# Look for: empty_llm_completion=0, slot_capture_enter>=1
```

**Offline replay (task #62 candidate — not yet built):**
- Pull raw STT utterances from this CallSid's rows in `call_events`
- Feed sequentially into `session_manager.run_user_turn()` with scripted-LLM harness
- Assert final state includes `actor._last_validated_phone == "+31625007600"` AND zero empty-completion events
- Cheaper than a real Twilio call for every regression sweep

## Files touched (all landed 2026-08-29)

- `packages/integrations/vertical_tools.py` — service alias resolver
- `packages/voice/speech_sanitizer.py` — tool-leak regex narrowed
- `packages/voice/barge_in.py` — BargeInPolicy (LK steal #5)
- `packages/core_agent/brain.py` — empty-completion watchdog + slot-prompt injection + humanness event emission
- `packages/observability/humanness_events.py` — typed event schema
- `packages/slot_parsers/slot_capture_prompts.py` — LK phone_number.py adaptation
- `apps/api/app/routes/twilio_actor.py` — `_stage_state_for_brain_dispatch` + `_on_policy_decision_callback`
- `apps/api/app/routes/trace.py` — GET `/trace/{call_id}` view
- `apps/api/app/routes/incident.py` — GET `/admin/calls/{call_id}/incident` view
- 40+ tests across `apps/api/tests/`

## Why this doc exists

Future refactor of the slot-capture path needs a named counterexample. If you're editing `packages/core_agent/brain.py`, `packages/slot_parsers/`, or `apps/api/app/routes/twilio_actor.py`, run `./scripts/trace_call.sh CA2fa1fef2065a7df388c3d6f58d7a7792` and verify the transcript still looks the same (baseline of the bug). Then when the code passes a real regression run, you know your change didn't accidentally rebreak this case.

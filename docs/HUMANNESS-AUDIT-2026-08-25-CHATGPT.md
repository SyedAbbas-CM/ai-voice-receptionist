# Humanness / Receptionist Capability Audit — 2026-08-25 (ChatGPT)

**Source:** ChatGPT audit of bundle `receptionist-codebase-2026-08-25_1312-audit-2026-08-25.zip`, prompted with `docs/CHATGPT-AUDIT-PROMPT-HUMANNESS-2026-08-25.md`.

**Verdict:** "Strong conversational architecture that is only partly being used." Not a model problem, not a prompt problem — a **wiring + capability** problem. Overall receptionist product rated ~5/10.

**Central finding (quoted):**
> You built `DialogueState`, acoustic signals, `ReactiveBrain`, `NextActionPolicy`, semantic plans, correction handling, commit guards, turn intent classification and tool infrastructure — but the production conversation is still largely **LLM reads transcript → improvises next reply**.

That's why your friend's "it isn't using its intelligence" complaint is correct.

**All three intelligence flags still False in `config.py:385, 469, 484`:**
- `dialogue_kernel_enabled: bool = False`
- `reactive_brain_enabled: bool = False`
- `next_action_policy_enabled: bool = False`

---

## Capability rating

| Area | Current |
|---|---:|
| Turn-taking / interruptions | 8/10 |
| Prompt/persona design | 7/10 |
| Conversation intelligence architecture | 8/10 design, ~4/10 live use |
| Human-like acknowledgement/listening | 5/10 |
| Memory / returning caller behavior | 3/10 |
| Scheduling | 5/10 |
| Human transfer | 2/10 |
| Message taking | 1/10 |
| Business operational knowledge | 4/10 |
| Post-call/operator workflow | 4/10 |
| **Overall receptionist product** | **~5/10** |

---

## P0 findings (must-fix for competitive humanness)

### 1. Wire the brain we already built
- **Files:** `packages/dialogue/next_action_policy.py` (marked "NOT WIRED TO RUNTIME" at :1-18); `packages/core_agent/prompt.py:98-102` (prompt itself admits "runtime does not currently pass explicit state fields"); flags in `apps/api/app/core/config.py:385, 469, 484`.
- **Target pipeline:** utterance → understanding → DialogueState → ConversationDecisionState → NextActionPolicy → ONE action → LLM verbalizes it → TTS.
- **Owner:** MINE. My A1/A2 wiring already exists inert (`next_action_synthesizer.py` for booking-confirm + slot-proposal). Needs activation + expansion to non-tool actions.

### 2. Semantic acknowledgment as a dialogue primitive
- Prompt already tells the LLM to vary ACKs by context (pain → "Ah, I see"; slot → "Yeah, Thursday works"; correction → "Oh sorry"; dictation → silent). But it's prompt guidance, not a primitive.
- **Target:** `ACK_NONE | ACK_LISTEN | ACK_UNDERSTOOD | ACK_CORRECTION | ACK_EMPATHY | ACK_AGREEMENT | ACK_TRANSITION | ACK_WAIT` — chosen by policy, verbalized by LLM.
- **Owner:** MINE.

### 3. Activate `ReactiveBrain` concept in controlled scenarios
- **Files:** `packages/core_agent/reactive_brain.py:1-14` (lanes: silent/backchannel/commit); `twilio_actor.py:4661-4681` (wired but gated on `reactive_brain_enabled=False`).
- **Progression:** hold/one-sec → obvious trailing-off → long explanations → lightweight backchannels → general operation.
- **Owner:** MINE (policy layer) + networking (twilio_actor gating).

### 4. `escalate_to_human` doesn't actually transfer
- **Files:** `packages/integrations/clinic_tools.py:373-382`; `restaurant_tools.py:394-402`. Both return `{"escalated": True, "callback_number": ...}` — no actual dial.
- **Target:** `TransferCoordinator` with `TransferDestination | TransferRule | TransferAttempt | TransferOutcome`, modes `BLIND | WARM | CALLBACK | MESSAGE_IF_FAILED`.
- **Comparison:** Vapi has blind + warm transfer with operator introduction, summary handoff, and return-to-original on failure. Table stakes.
- **Owner:** SHARED — I own the tool/prompt surface, networking owns the Twilio outbound dial + call-conference primitives.

### 5. No `take_message` tool exists
- **Verified:** grep for `take_message` / `capture_message` / `ReceptionMessage` in packages/ + apps/ returns zero non-test hits.
- The prompt tells callers "I can help with bookings, questions, or **messages**" — that's a lie right now.
- **Target:** `ReceptionMessage` model (tenant / call_id / caller / phone / recipient / department / subject / message / priority / callback_requested / preferred_callback_time / status / created_at) + priority routing (normal → inbox/email; urgent → inbox + SMS; emergency → transfer).
- **Comparison:** ElevenLabs treats this as core.
- **Owner:** MINE (tool + prompt) + networking (schema).

### 6. Call-ending is a timer puzzle, not a semantic action
- **Files:** `twilio_actor.py:660-687` — many special flags (`_idle_task`, `_idle_prompted`, `_caller_spoke_since_farewell`, `_arming_from_idle_loop`, farewell tasks, silence windows).
- **Target:** `END_CALL` action from policy → generate farewell → speak → Twilio confirms playout → hangup. Interruption during farewell cancels back to ACTIVE.
- **Owner:** SHARED — I own the `END_CALL` action semantics, networking owns the state-machine cleanup in twilio_actor.

### 7. Real calendar is materially weaker than demo calendar
- **Files:** `packages/integrations/fake_calendar.py` has `find_by_phone`, `cancel`, `reschedule` (:159, :188, :204). `packages/integrations/google_calendar.py` has ONLY `is_available` + `list_slots` + `book`.
- **Consequence:** clinic_tools exposes cancel/reschedule to the LLM, but they only work in demo. Real customer's Google Calendar can't be modified through the tool loop.
- Also: `BusinessProfile` (`packages/schemas/business.py:36-63`) models nothing beyond hours, services, FAQs, one escalation phone. No staff, locations, resources, time off, holidays, buffer times, service-to-staff assignments.
- **Target:** proper scheduling domain (staff / staff→service / locations / resources / time-off / holidays / buffers).
- **Comparison:** ElevenLabs models all of these.
- **Owner:** MINE (Google Calendar client + business schema extension) + networking (persistence for staff/location tables).

---

## P1 findings (competitive parity)

### 8. Returning callers should feel known
- ANI is available but no `CallerResolver` fetches known-client context to feed conversation.
- **Target:** `CallerContext { client_id / name / known_phone / preferred_language / upcoming_appointments / last_appointment / open_message / last_call_summary / preferred_staff / recent_unresolved_issue / CRM_profile }` injected pre-turn.
- **Owner:** MINE (CallerResolver + prompt injection) + networking (DB query surface).

### 9. Acoustic/emotional intelligence built but unused
- **Files:** `packages/dialogue/acoustic.py:48-98` computes speaking_rate / energy / pauses / interruptions / repeated_phrases / ASR_confidence → derives `urgency_score` / `frustration_signal` / `asr_uncertain`.
- **Files:** `next_action_policy.py:74-112` already understands `RUSHED | CASUAL | CONFUSED | FORMAL | UPSET | ANXIOUS`.
- **Gap:** they don't connect. Rushed caller → CRISP delivery; frustrated → acknowledge problem + repair fast; halting → don't jump silences.
- **Owner:** MINE.

### 10. Knowledge gaps should feed back into KB
- **Target:** low-confidence RAG → "I don't want to give wrong info" → offer message/transfer → log as `KnowledgeGap` → dashboard → owner answers once → KB improves.
- **Comparison:** ElevenLabs' operator inbox does exactly this loop.
- **Owner:** MINE (RAG + KB feedback) + networking (KnowledgeGap table).

### 11. Business-hours behavior needs to be operational, not just factual
- Current `BusinessHours` = single range per weekday.
- **Target:** split hours (9-12, 1-5), holiday closure, temporary closure, staff sick, after-hours emergency line, weekend policy, STAFF-FIRST during office hours (ring 15s, no answer → AI), AI-first after hours.
- **Comparison:** ElevenLabs exposes staff-first + transfer rules + blocked-callers.
- **Owner:** MINE (schema + policy) + networking (DB).

### 12. DTMF is better than expected but not first-class
- Currently structured-data fallback (phone-number capture). Not general (navigate other IVR / press extension / enter PIN / dial department).
- **Owner:** deferred — good backlog, not urgent for inbound dental.

### 13. Multilingual has groundwork but no live workflow
- Nova-3 configured for multiple languages, `llm_capabilities.py` has locale-aware model selection.
- Missing: language detection → STT language routing → conversation locale → LLM response → matching TTS voice/language, all live.
- **Comparison:** ElevenLabs = 70+ languages with automatic switching.
- **Owner:** SHARED — I own the language-detection + routing logic, networking owns TTS provider swap surface.

### 14. Build a proper Receptionist Inbox
- Currently: fragments (call logs, transcripts, bookings, CRM output, some disposition). Not one coherent surface.
- **Target daily view:** TODAY (N calls, N bookings, N messages, N transferred, N urgent, N knowledge gaps) + timeline of each call with outcome + click for transcript + click for message content.
- **Owner:** MINE (dashboard/admin routes — extends the dashboard v1 I already shipped).

---

## What ChatGPT explicitly said to KEEP

**Prompt design is already good.** Don't add another 5 pages of persona instructions. Reaching the point where anything sufficiently important should become code/state/policy, not another prompt rule.

Specific praise:
- 10-25 word phone turns
- one question at a time
- no bullet-list voice dumps
- don't reflexively say "Sure!"
- contextual acknowledgements
- don't interrupt dictation
- adapt to rushed/confused/upset callers
- drop your sentence when interrupted
- ambiguous "okay" is not consent
- don't expose tool names/system internals

## What ChatGPT called out as GOOD architecture

- Turn-taking / interruptions (8/10)
- The building blocks: DialogueState, NextActionPolicy, ReactiveBrain, SemanticPlan, acoustic features, correction handling, commit guards
- The prompt persona design (7/10)
- Test suite: 191/191 pass on core conversational/intelligence slice

Direct quote:
> "You do not need to throw away the custom system for Vapi to get intelligent receptionist behavior. The repo already contains unusually good building blocks. Vapi currently beats it primarily because those behaviors are finished, wired and exposed as reliable primitives. Your next breakthrough is integration and product semantics — not another model swap or another giant system prompt."

---

## Recommended 4-phase plan (ChatGPT's ordering — I agree)

**Phase 1 — Make intelligence actually control conversation**
- Wire DialogueState → NextActionPolicy → realizer
- Activate ReactiveBrain in controlled scenarios first (silence/backchannel-on-hold)

**Phase 2 — Complete receptionist fundamentals**
- Real `END_CALL` action (not timer puzzle)
- `TransferCoordinator` (blind + warm)
- `TakeMessage` tool + `ReceptionMessage` model
- Failed-transfer fallback

**Phase 3 — Make it know the business**
- Staff, locations, departments, schedules, holidays
- Provider/service relationships
- Returning-caller context

**Phase 4 — Build the operating loop**
- Receptionist inbox
- Standardized call outcomes
- Knowledge gaps → owner answer → KB improve
- Follow-up tasks + CRM read/write context

---

## AI identity — cross-cut with backend audit

ChatGPT humanness view aligns with what I already shipped: current prompt correctly forbids "Yes, I am an AI language model..." (bad), redirects to virtual receptionist (good). Utah §13-77-103 requires disclosure when directly asked (the backend audit finding). My prompt update on 2026-08-25 handles this correctly.

But ChatGPT recommends taking it OUT of general LLM improvisation entirely — build `IdentityDisclosurePolicy` selecting the required answer per tenant/jurisdiction. That's a Phase 3 concern.

---

## Test-suite findings from the audit

**Clean core slice:** 191/191 passed (ReactiveBrain, NextActionPolicy, next-action synthesizer, dialogue acoustic features, dialogue kernel, commit coordinator, barge-in classifier).

**Wider slice:** 206 passed, 7 failed, 6 errors. Errors mostly from missing `phonenumbers` in audit env (correctly listed in `apps/api/requirements.txt`). Failures are stale `ScriptedLLM.complete()` not accepting `site="extractor"` kwarg — matches the same 19 baseline failures I noted 2026-08-24. Fix them because those are the exact behaviors we need confidence in.

---

## Comparison table — target capabilities

| System | Current | Target |
|---|---|---|
| Answer FAQs | ✅ | ✅ |
| Book appointment | ✅ | improve |
| Cancel/reschedule | ⚠️ fake only | **full production** |
| Natural interruptions | ✅ strong | polish |
| Contextual ACKs | ⚠️ prompt-driven | **policy-driven** |
| Silence/"let me think" | ⚠️ built/off | **enable** |
| Human transfer | ❌ | **warm + blind** |
| Take message | ❌ | **build** |
| End call deliberately | ⚠️ fragile | **explicit END_CALL** |
| Caller history | ❌ | **build** |
| Staff routing | ❌ | **build** |
| Departments | ❌ | **build** |
| Multi-location | ❌ | **build** |
| Provider scheduling | ❌ | **build** |
| Holidays/time off | ❌ | **build** |
| After-hours policy | ⚠️ | **build** |
| Staff-first overflow | ❌ | **build** |
| Knowledge gaps | ❌ | **build** |
| Inbox | ❌ | **build** |
| Multilingual switching | ⚠️ groundwork | **productize** |
| DTMF data capture | ✅ | expand |
| Voicemail handling | ⚠️ outbound pieces | **build primitive** |
| SMS/email followup | ✅ | harden |
| CRM | ✅ write-heavy | add read context |
| Call outcomes | ⚠️ partial | **standardize** |
| Business rules | prompt-heavy | **structured rules** |

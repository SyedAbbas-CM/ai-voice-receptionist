# PHASE 5 — Differentiators Plan

**Goal:** the features that turn "another voice agent" into "controllable voice-agent OS." Doc #56 line 46: this is the demo-defining stuff.

**Position:** blocks on PHASE2 (event log rich enough) and parts of PHASE4b (CRM data for cockpit views).

## Global constraints
- **These are 5 sub-tracks; ship in parallel if manpower allows.** Only cockpit is sequentially blocked on event-log richness.
- **Nothing here is a rewrite of PHASE1-4.** Additions and observability layers only.
- **Adaptive router NEEDS PHASE1's per-provider metrics** — do not attempt without them.
- **Shadow supervisor NEEDS a cost math decision** — 2× LLM cost may be untenable; doc #56 line 80.

## Sub-track 5.1 — Operator Cockpit

**Doc #56 line 46:** "the demo-defining feature. Not because it's technically hard (it isn't) but because it changes the sales conversation from 'another agent' to 'controllable runtime.'"

### Prereqs
- Per-call event log has: turn boundaries, tool calls + results, kernel decisions, gate holds/drops, commit-lock claims (all exist post-PHASE2)
- CRM data flowing (PHASE4b) so cockpit "who called" list is meaningful

### Tasks
- [ ] **1.** WS endpoint `/ops/live-calls` — streams per-call events as they happen
- [ ] **2.** React frontend (`apps/ops-cockpit/`) — Next.js on Vercel, calls the WS
- [ ] **3.** View: live-calls list (concurrent calls, state per call, elapsed time)
- [ ] **4.** View: single-call drill-down (waveform + transcript + timeline + tool calls)
- [ ] **5.** Take-over button: dispatch handoff-to-human event to actor; caller hears "connecting you to a teammate"
- [ ] **6.** Mute button: mutes agent for the current turn (operator can override with typed reply)
- [ ] **7.** Kill-switch: hangup + log reason
- [ ] **8.** Post-call review: outcome + transcript + tool-call audit + operator can flag "this went wrong"
- [ ] **9.** Flag → auto-add to failure→regression compiler DSL (see sub-track 5.2)
- [ ] **10.** Auth (ops-team login, per-tenant scoping)
- [ ] **11.** 20-call soak with an operator watching + intervening
- [ ] **12.** Close-out doc

## Sub-track 5.2 — Failure→Regression Compiler

**Doc #56 line 42:** "we just spent 6 hours diagnosing Hamzah with grep. If HAMZAH-001 existed as an automated scenario and CI blocked deploys when it regressed, R1-R5 would have been mechanical fixes."

### Prereqs
- Existing SOAK harness (`scripts/replay-audio.py` + `scripts/verify-call.sh`) — good starting point
- Per-call event log stable

### Tasks
- [ ] **1.** DSL design: `packages/regression/dsl.py` — YAML shape:
  ```yaml
  scenario: HAMZAH-001
  audio: fixtures/hamzah-fake-wait.wav
  asserts:
    - kind: no_marker
      pattern: ZOMBIE_SPEAKING
    - kind: exact_count
      pattern: TTS_STREAM_START
      count_expr: "== stt_final_count"  # one reply per turn
    - kind: state_at_end
      value: LISTENING
    - kind: max_gap_ms
      between: [stt_final, tts_first_byte]
      value: 5000
  ```
- [ ] **2.** Runner: `apps/api/scripts/regression-run.py <scenario.yaml>` — pipes audio through actor, evaluates asserts, exits 0/1
- [ ] **3.** Golden library: `apps/api/tests/regression/*.yaml` — start with 10 scenarios from every reliability fix (R1 zombie, R2 fake-wait, gate drops, commit lock, phone precondition, DTMF, ANI, POSSIBLE-confirm)
- [ ] **4.** CI wire-up: GH Action that runs the whole golden library on every PR; blocks merge on any red
- [ ] **5.** Operator cockpit "flag as regression" button → auto-generate DSL YAML from the event log + prompt for asserts
- [ ] **6.** Test coverage of the runner itself
- [ ] **7.** Close-out

## Sub-track 5.3 — Shadow Supervisor

**Doc #56 line 80 warning:** two model calls per turn = 2× LLM cost. Decide first.

### Prereq
- Cost decision documented in `docs/decisions/2026-XX-XX-shadow-supervisor-cost.md`

### Two options based on cost decision:

**Option A (LLM-based, if cost OK):**
- [ ] **1.** Second LLM instance runs same turn context + supervisor prompt ("would the primary's reply be safe to speak?")
- [ ] **2.** Async — doesn't gate primary reply; posts to a supervisor log
- [ ] **3.** If supervisor BLOCK → out-of-band SMS to operator "call CA<sid> flagged"
- [ ] **4.** No barge-in on primary — primary reply already spoken; supervisor is a review layer

**Option B (rule-based, if cost NOT OK):**
- [ ] **1.** Ship the deterministic rules doc #56 line 80 identified: block booking without receipt, block payment without confirmation, block factual claim without KB citation
- [ ] **2.** Wire into SpeechCommitGate as an additional class (`UNSUPPORTED_COMMITMENT`)
- [ ] **3.** Covers ~80% of the win at 0% cost

## Sub-track 5.4 — Adaptive Provider Router

**Prereq:** PHASE1 latency lab produced per-provider metrics.

### Tasks
- [ ] **1.** Health check per provider (existing `LLMRouter` has this, extend)
- [ ] **2.** Score = weighted avg(latency, error_rate, cost, quality_score)
- [ ] **3.** Adaptive: reshuffle preference order every N minutes based on rolling metrics
- [ ] **4.** Circuit breaker: after 3 consecutive failures, provider ejected for 5min
- [ ] **5.** Test with synthetic provider failures
- [ ] **6.** Close-out

## Sub-track 5.5 — Cross-Channel Continuation

**Doc #56 line 70:** big rebuild. WhatsApp + Telegram code exists but hasn't been maintained since Sprint 6.

### Tasks
- [ ] **1.** Audit `packages/channels/` — what's still working?
- [ ] **2.** State: `CallState.channel_thread_id` — same conversation across voice + SMS + WhatsApp
- [ ] **3.** Kernel awareness: continuing a booking flow that started on voice, resumed via WhatsApp
- [ ] **4.** Templates per channel (voice = TTS, SMS = 160-char, WhatsApp = rich)
- [ ] **5.** Test: booking flow started on call → continues via SMS reply → confirms via WhatsApp reaction
- [ ] **6.** Close-out

## Success criteria (whole phase)
- Operator cockpit deployed, operators can watch and intervene
- ≥20 regression scenarios in golden library; CI blocks regressions
- Shadow supervisor OR rule-based safety layer live
- Adaptive router shifting providers based on real metrics
- One cross-channel flow works end-to-end (call → SMS → WhatsApp)

# SOAK Test Scenarios (task #362)

Purpose: exercise the reliability changes shipped since Sprint 12 (R1
zombie SPEAKING, R2 fake-wait, R4 backchannel cache, R5 turn-stall
watchdog, R3 structured input, SpeechCommitGate, one-gen-one-commit
lock, DTMF/ANI, phone precondition) on **real calls** before we run
the latency tournament.

Each scenario has:
- **Setup**: how to make the call (which number to dial, script the
  agent is running)
- **Steps**: what the caller says/does
- **Pass markers**: log lines that MUST appear
- **Fail markers**: log lines that MUST NOT appear
- **Verify**: run `scripts/verify-call.sh CA<sid>` after hangup

Every scenario is scored automatically by the verify script.  Manual
listening judgment is a separate column — a pass on markers with a
subjectively bad call is still a fail overall.

Target: 20-30 real calls covering all 8 scenarios (~3 each, +
freeplay).  Zero criticals allowed before PHASE1 latency tournament.

---

## Scenario 1: fake-wait guard (Hamzah replay)

**Setup**: any call, no booking intent.

**Steps**:
1. Caller: "Hi, uh, I have a question about your services"
2. Wait — LLM should reply immediately with a follow-up question,
   NEVER "one moment" / "let me check" without a tool call.

**Pass markers**:
- `SPEAKING→LISTENING` after the reply (R1 epilogue)
- No `ZOMBIE_SPEAKING` warn
- `FAKE_WAIT_BLOCKED` in the log IF the LLM tried it (proves R2 guard fired)

**Fail markers**:
- Any 15s+ gap between STT final and TTS start
- `TURN_STALLED` at ERROR level
- Two `TTS_STREAM_START` events same gen without a `STREAM_REPLY_REPLACED` in between

---

## Scenario 2: gen=N one-commit invariant (Abdullah replay)

**Setup**: any call.

**Steps**:
1. Caller: "What times work for tomorrow?"
2. Wait for reply.
3. Caller (mid-reply): "Actually, wait" — force a barge candidate.
4. Immediately follow up: "What others are available?"

**Pass markers**:
- `COMMIT_LOCK_CLAIM` (at least one)
- `COMMIT_LOCK_SKIP` if a race actually happened (fine — the guard
  worked)

**Fail markers**:
- Two `TTS_STREAM_START` events with same `gen=N` and NO
  `STREAM_REPLY_REPLACED` between them (that's the Abdullah bug —
  distinct texts stacking on the same gen).

---

## Scenario 3: phone dictation (voice only, PK number)

**Setup**: Karachi tester dials in.  Tenant configured US default,
accepted=[US, PK].

**Steps**:
1. Caller: "I'd like to book an appointment"
2. LLM asks for name → caller gives name
3. LLM asks for phone → caller says: "zero, triple three, five two
   four four, seven seven two"
4. LLM confirms, books.

**Pass markers**:
- `SLOT_CAPTURE_ENTER call=... kind=phone` (once R3 phase 4 workflow
  wire lands — for slim v1, this scenario is instead scored by:)
- `book_appointment` tool result with `booked: true` AND
  `event.phone: "+923335244772"` (canonical E.164)

**Fail markers**:
- Any `phone_invalid` / `phone_partial` / `phone_too_long` result on
  the FINAL booking attempt (retries are fine as intermediate)
- LLM saying "you're all set" without a `booked:true` receipt (that's
  the fake-booking guard territory)

---

## Scenario 4: DTMF mid-capture

**Setup**: Karachi tester dials in.

**Steps**:
1. Caller: "book me for tomorrow morning"
2. LLM asks for phone.
3. Caller says: "zero three three three" (4 digits by voice)
4. Caller keys: 5-2-4-4-7-7-2 (7 more digits by DTMF)
5. Booking should proceed with the combined number.

**Pass markers**:
- `DTMF_FEED_SLOT` (at least 7 — one per digit)
- Same `book_appointment` success as scenario 3

**Fail markers**:
- `DTMF_IGNORED` during the phone-collection window (slot didn't open)

---

## Scenario 5: ANI accept

**Setup**: Karachi tester dials in.  Their caller ID appears in
`start.customParameters.from`.

**Steps**:
1. Caller: "book me for tomorrow, use the number I'm calling from"
2. LLM should offer/accept the ANI (`+923...`) as the phone.
3. Booking proceeds.

**Pass markers**:
- `ANI_RESOLVED call=... raw=... status=valid value=+92...`
  (fires when workflow calls `resolve_ani_candidate`)
- `book_appointment` with `event.phone` = the ANI

**Fail markers**:
- LLM asks for the number verbatim anyway (ANI ignored)
- ANI committed WITHOUT caller confirmation (violates spec: ANI is a
  candidate, never auto-truth)

---

## Scenario 6: POSSIBLE requires confirmation

**Setup**: any call.

**Steps**:
1. Caller: "book me at 8am tomorrow, phone is 555 123 4567" (a
   number libphonenumber may classify as POSSIBLE-not-VALID
   depending on allocation tables at test time).

**Pass markers**:
- If validator returns POSSIBLE: LLM should ask a confirmation
  question OR the tool returns a precondition prompt.
- Booking only proceeds after explicit "yes" from caller.

**Fail markers**:
- POSSIBLE result auto-committed to `booked: true` on FIRST turn (no
  confirmation loop).

---

## Scenario 7: stall recovery (silence mid-capture)

**Setup**: Karachi tester dials in.

**Steps**:
1. LLM asks for phone.
2. Caller: "zero three three three"
3. Caller stays SILENT for 8-10 seconds.
4. Agent should prompt gently (stall stage=first_prompt), then
   escalate.

**Pass markers**:
- `SLOT_STALL first_prompt` (once slot capture is wired into workflow)
- Agent audibly re-prompts within 6-8 seconds

**Fail markers**:
- Agent goes fully silent for 15s+ (idle-followup hasn't fired either)
- `TURN_STALLED` at ERROR

---

## Scenario 8: streaming happy path (baseline)

**Setup**: any call.

**Steps**:
1. Caller: "hi, tell me about your practice"
2. LLM streams a multi-sentence reply.

**Pass markers**:
- `TTS_SENTENCE_QUEUED` fires for each sentence
- `TTS_STREAM_START` count matches sentence count
- All released via gate as SAFE (`GATE_RELEASE` or immediate stream)
- `GATE_DROP` count = 0

**Fail markers**:
- Any `GATE_DROP` (nothing should get dropped on a benign reply)
- `SPEECH_GATE_DROPPED` line

---

## After every call: run verify-call.sh

```bash
# CallSid is in the `TWILIO_START` line or in per-call log filename.
apps/api/scripts/verify-call.sh CA<32-hex>
```

Output is a scorecard per scenario marker (pass/fail/notseen).  Roll
up 20-30 calls into a spreadsheet; any red requires investigation
before the reliability soak gate is passed.

## What to do on failure

1. Get the per-call log: `apps/api/data/logs/calls/CA<sid>.log`
2. Get the surrounding uvicorn log: search
   `apps/api/data/logs/uvicorn-<date>.log` for the CallSid.
3. Bundle both + the scenario + the observed audio behavior +
   `scripts/verify-call.sh` output → send to me for triage.

## What "passing" the soak means

- 20+ calls across the 8 scenarios above
- Every scenario has at least 2 successful runs
- Zero critical failures (fake booking, zombie state, silent agent >15s)
- Playback quality is subjectively acceptable

Only then do we start PHASE1 (latency tournament).

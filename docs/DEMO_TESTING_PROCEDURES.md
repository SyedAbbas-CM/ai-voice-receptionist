# Demo Testing & Failure-Triage Procedures

Last updated: 2026-08-04 (after Sprint 10 streaming wiring)

The point of this doc: when the demo breaks — and it will — this is
the runbook that gets us from *"the voice sounded weird"* to
*"backchannel classifier is over-firing on caller filler"* in five
minutes.

## Pre-demo checklist

Before the phone rings, verify every layer is up.  Under 2 minutes.

```bash
# 1. All 5 LLM providers responding
python scripts/bench_llms.py 2>&1 | grep -E "^\s+\d+ms"

# 2. Server + healthcheck
curl -sf http://127.0.0.1:8000/health | jq .

# 3. Every intelligence flag ON
curl -s http://127.0.0.1:8000/debug/config | jq .intelligence_flags

# 4. Capability table loaded — MAIN_BRAIN has ≥3 approved models
curl -s http://127.0.0.1:8000/debug/capabilities | jq '.per_operation.main_brain | length'

# 5. Metrics endpoint returning
curl -sf http://127.0.0.1:8000/metrics | head -20

# 6. Tunnel is reachable from Twilio
curl -sf https://<your-tunnel>/health | jq .
```

Expected `intelligence_flags`:
```json
{
  "twilio_use_actor": true,
  "two_planner_enabled": true,
  "two_stage_barge_in_enabled": true,
  "dialogue_kernel_enabled": true,
  "streaming_stt_enabled": true,
  "turn_manager_enabled": true,
  "telephony_output_gain_db": 6.0
}
```

If any is false — flip in `.env` and restart before the demo.

## During the call — what to watch

Open two terminals:

**Terminal A** — tail the server log filtered by INFO+:
```bash
tail -f /path/to/server.log | grep -E "INFO|WARN|ERROR"
```

**Terminal B** — poll metrics every 2s:
```bash
watch -n 2 "curl -s http://127.0.0.1:8000/metrics | grep -E 'voiceops_(turn|stream|stage1|barge|two_planner)' | head -20"
```

## After the call — 60-second debrief

Grab the `session_id` from the Twilio start log or server startup.
Then:

```bash
# 1. Semantic timeline of the whole call — one glance shows the story
curl -s http://127.0.0.1:8000/debug/call/$SESSION_ID/timeline | jq .

# 2. Raw event log if you need the deep dive
curl -s http://127.0.0.1:8000/debug/call/$SESSION_ID | jq .

# 3. Any classified errors on this call
curl -s http://127.0.0.1:8000/debug/failures/call/$SESSION_ID | jq .

# 4. Recent error rollup (are we seeing patterns across recent calls?)
curl -s "http://127.0.0.1:8000/debug/failures/patterns?hours=1" | jq .
```

---

## Failure triage by symptom

### "The agent sounded weird / robotic / quiet"

1. Check `telephony_output_gain_db` — should be 6.0.
2. Check `voiceops_two_planner_hit_total` — hit=false > hit=true means
   the perf planner is timing out and delivery defaults are firing.
3. Check the timeline for `stream_failed` events — streaming STT
   fallback runs batch mode with no perf tuning.

### "Agent didn't hear me / didn't respond"

Check the timeline:
- No `stt_partial` events → **audio isn't reaching Deepgram**.  Check
  DEEPGRAM_API_KEY, network to `wss://api.deepgram.com`, and
  `voiceops_stream_event_total{kind="stt_partial"}` counter.
- `stt_partial` but no `stt_final` → **endpointing not firing**.
  Caller may be talking too continuously; Deepgram waits for 300ms
  silence.  Verify `endpointing=300` in the WS URL.
- `stt_final` but no `end_of_turn` → **turn manager not consuming**.
  Check `TURN_MANAGER_ENABLED=true`.
- `end_of_turn` but no brain reply → **LLM router failed**.  Look
  for `provider_outage` in `/debug/failures/call/$ID`.

### "Agent interrupted me mid-sentence"

Timeline should show `interruption` events under agent-turn generations.
Check:
- `voiceops_turn_event_total{kind="interruption"}` vs
  `{kind="backchannel"}` — if backchannel count is 0 and every caller
  sound triggers interrupt, backchannel classifier isn't matching.
- Look at the STT partial text in the timeline — is Deepgram returning
  garbled transcripts?
- Adjust `TurnManagerConfig.interruption_confirm_partials` upward if
  false-positives dominate.

### "Agent said 'sure!' after I said 'give me a second'"

- Verify `TURN_MANAGER_ENABLED=true`.
- Check timeline for `user_requested_pause` event on that turn.  If
  present but agent still responded, `_on_turn_event_pause` didn't
  run — inspect handler registration.
- If NOT present, `classify_short_utterance` didn't match — see the
  patterns in `packages/runtime/turn_manager.py::_PAUSE_PATTERNS`
  and extend.

### "Agent booked the wrong day / duplicated a booking"

- Check timeline for `state` events showing slot supersession.
  Look for `slot_recorded start_iso` with the two different values.
  Both should appear; the older one marked SUPERSEDED.
- If `commit` outcome is SUCCESS twice for the same booking, the
  idempotency key differs → check that `ActionProposal.build` is
  seeing identical args (evidence_turn_id can drift).
- If `commit` outcome is REJECTED with `evidence_invalidated`, the
  correction landed BEFORE commit — that's the guardrail working.

### "Agent hallucinated a date / made up a phone number"

- Look for `state` events with `SlotEvidence` records — those show
  the source_turn_id.  If the agent spoke a value that has no
  corresponding evidence, prompt drift.
- Check `/debug/failures/patterns?hours=1` for TEMPORAL or
  ARG_NORMALIZATION clusters.
- Check the RAG bundle: `curl -s "$BASE/debug/call/$ID/timeline"
  | jq '.timeline[].events[] | select(.source=="tool" and
    .kind=="lookup_answer")'`

### "The agent went silent for 3+ seconds mid-turn"

- Turn-latency histogram: `curl -s /metrics | grep
  voiceops_turn_latency_seconds`.  p95 should be < 1500ms.
- Check `voiceops_provider_fallback_total` — if bumping, LLM router
  is chaining fallbacks and paying cool-down time.
- Check `stream_failed` in timeline — if streaming STT died, we
  reverted to batch which adds ~800ms per turn.

### "It just crashed"

```bash
# Classified error rollup
curl -s "http://127.0.0.1:8000/debug/failures/patterns?hours=1" | jq .
```

The `top_action` field tells you where to look first.  Then grab
the raw traceback from server logs by matching call_id + the
timestamp of the highest-count cluster.

---

## Metric quick-reference

| Metric | What healthy looks like |
|---|---|
| `voiceops_turn_latency_seconds` | p50 < 700ms, p95 < 1500ms |
| `voiceops_two_planner_hit_total{hit="true"}` | ≥ 80% of turns |
| `voiceops_stage1_duck_total{outcome="false_trigger"}` | < 30% of ducks |
| `voiceops_barge_in_total` | matches real caller interruptions |
| `voiceops_backchannel_total` | > barge_in when caller uses filler words |
| `voiceops_heard_vs_generated_ratio` | 1.0 for uninterrupted turns |
| `voiceops_stream_event_total{kind="stt_final"}` | ≥ turns_taken |
| `voiceops_turn_event_total{kind="end_of_turn"}` | ≥ turns_taken |
| `voiceops_provider_fallback_total` | 0 during a demo |

---

## Rollback plan

Everything is behind a flag.  If streaming misbehaves and you have
1 minute, flip these in `.env` and restart:

```env
STREAMING_STT_ENABLED=false
TURN_MANAGER_ENABLED=false
```

That drops the streaming pipeline and reverts to the batch STT +
VAD-silence path.  Costs ~800ms per turn but is more predictable.

If two-planner is the problem:
```env
TWO_PLANNER_ENABLED=false
```

If the kernel is the problem (unlikely, it's mostly additive):
```env
DIALOGUE_KERNEL_ENABLED=false
```

Absolute nuclear rollback:
```env
TWILIO_USE_ACTOR=false
```

That reverts to the pre-Sprint-9 batch handler.  Nothing intelligent,
but stable.

---

## What's NOT covered by this doc

- **STT accuracy per accent/noise** — no proxy metric; recorded
  fixtures + Sprint 10 E audio evaluation lab will address.
- **Voice quality per TTS provider** — subjective; we listen.
- **Booking-actually-arrived checks** — Google Calendar-backed
  bookings need a manual `gcloud calendar events list` after each
  demo booking to confirm.

---

## Sprint 10 E — the missing lab

Everything above is *reactive* triage.  Sprint 10 Track E (audio
evaluation lab) is what turns this into *proactive* regression
testing: recorded 8kHz WAV fixtures + deterministic state assertions +
per-scenario replay.  Not blocking for the demo — but book it as
next-post-demo work.

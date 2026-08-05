# Audit Verification — 2026-08-05

**Verifying:** `docs/AUDIT_2026-08-05-runtime-failure-patterns.md`
**Method:** grep + read exact code locations against auditor claims + run pytest.

## Test-suite state (my run, after Sprint 11 fixes)

- **930 passed**
- **4 failed** — all real, all traceable to my recent changes:
  - `test_turn_manager.py::test_first_final_fires_eager_end_of_turn` — I broke by requiring `speech_final=True` for promotion; test sends `speech_final=False`
  - `test_turn_manager.py::test_final_then_no_resume_promotes_to_end_of_turn` — same cause
  - `test_turn_manager.py::test_speech_resume_after_final_fires_turn_resumed` — same cause
  - `test_twilio_actor_two_stage_barge.py::test_ducked_state_skips_outbound_media_frames` — stale reference to renamed method
- **35 skipped** — environmental (Cartesia SDK, sqlite-vec, num2words)
- Auditor saw 28 failures because the audit ran on the pre-Sprint-11 snapshot (before my `speech_final` + `_send_mulaw_frames→_send_audio_frames` changes).

## P0 verification

### ✅ P0-1: Actor mailbox awaits handler → blocked during long work
`packages/runtime/call_actor.py:310`
```python
await handler(self, event)
```
Confirmed. `_on_turn_event_end` in `twilio_actor.py` fires the brain and awaits it via `_run_brain_from_text` → `session_manager.run_user_turn` → LLM + tools + `_speak` (which itself awaits TTS + full playback). Mailbox blocked for entire turn.

### ✅ P0-2: bump_turn drains mailbox from inside handler
`packages/runtime/call_actor.py:bump_turn`:
```python
await self._drain_mailbox()
```
Confirmed. Comment even says "Waits for the current mailbox to drain first." Called from `_on_turn_event_end:1015-1017` which IS a mailbox handler. Deadlock-with-timeout.

### ✅ P0-3: Two barge systems active on same frame
`apps/api/app/routes/twilio_actor.py`:
```python
Line 334:  self._stt_bridge.feed(mulaw_frame)     # StreamingSTTBridge
Line 337:  await self._buffer_barge_frame(mulaw_frame)   # legacy batch VAD
```
Confirmed. Every inbound frame goes to both.

### ✅ P0-4: _on_final always emits INTERRUPTION when speaking
`packages/runtime/turn_manager.py`:
```python
if self._agent_is_speaking():
    await self._emit(TurnEventKind.INTERRUPTION, text=text, is_final=True)
    return
```
Confirmed. No check for prior BACKCHANNEL/PAUSE classification on the partial that led here.

### ✅ P0-5: Model tool args recorded as caller evidence
`packages/core_agent/kernel_wiring.py::_record_slot_evidence`:
```python
source_role: SourceRole = SourceRole.CALLER,   # default
confidence: float = 0.85,
status: SlotStatus = SlotStatus.EXPLICIT,
```
Confirmed. If a tool-call handler feeds LLM-proposed args through this, they're labeled as caller-explicit.

### ✅ P0-6: Direct tool_handler bypasses commit coordinator
`packages/core_agent/brain.py:334`:
```python
result = await self.tool_handler(tc)
```
Confirmed. `try_commit_booking` exists in `kernel_wiring.py:343` but the brain doesn't route booking tools through it.

### ✅ P0-7: Deepgram producer/consumer swallow errors
`apps/api/app/providers/stt/deepgram_stt.py`:
- Producer: `except Exception as e: log.warning("deepgram producer failed: %s", e)` — falls into finally, sends CloseStream, returns normally.
- Consumer: `except Exception as e: log.warning("deepgram consumer failed: %s", e)` — falls into finally, puts `None` sentinel.

Bridge sees normal completion. No `stream_failed` event. No reconnect triggered.

## P1 verification (spot-checked)

### ✅ P1-4: Audio duration hardcoded to 16kHz PCM
`twilio_actor.py:825-828`:
```python
duration_ms=int(len(audio_bytes) / 32),  # 16kHz s16le = 32 bytes/ms
```
Yes — magic 32, no format metadata.

### ✅ P1-8: Streaming queue drops incoming (not oldest) on overload
`streaming_stt_bridge.py:129-134`:
```python
try:
    self._audio_queue.put_nowait(payload)
    ...
except asyncio.QueueFull:
    if self._audio_queue.qsize() % 100 == 0:
        log.warning("STT bridge queue full ...")
```
Yes — `put_nowait` raises on full, we log and drop the NEW frame, keeping stale audio.

### ✅ P1-10: Idle followup hardcoded + armed in finally
`twilio_actor.py`:
```python
_IDLE_FAREWELL: str = "Alright, thanks for calling Smile Dental. Have a great day!"
```
Hardcoded. And `_speak`'s `finally: self._arm_idle_followup()` arms even after cancellation.

### ✅ P1-11: Deepgram hardcoded to en-US
`deepgram_stt.py:93`:
```python
"language": "en-US",
```
Yes.

## Conclusion

**Every P0 verified as real.** Auditor's diagnosis is accurate. The right response is Sprint 12: authoritative non-blocking actor architecture — not more symptom patches. See `docs/superpowers/plans/SPRINT_12_authoritative_actor.md` for the plan.

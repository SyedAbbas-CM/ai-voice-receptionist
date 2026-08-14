# Phase 1 — Latency Lab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** measure — don't guess — the p50 / p95 / cost / accuracy of 4 candidate architectures on the same recorded phone audio, so PHASE2 picks the winner instead of trusting doc #55's ambition.

**Architecture:** offline harness that replays a corpus of caller-side WAVs through each pipeline configuration, records per-turn metrics, aggregates into a comparison table.

**Tech Stack:** Python (existing repo), `replay-audio.py` (already exists on `chore/soak-harness`), Deepgram Flux (existing), OpenAI Realtime API (new), ElevenLabs Multi-Context WS (existing wrapper — needs re-verification), local models on 2×3090 rig.

## Global Constraints

- **Nothing ships from this phase.** It's a benchmarking harness. The winning pipeline gets wired in PHASE2 sub-phase 2c, not here.
- **Sanity gate MUST be closed first** — see `docs/rnd-2026-08/59-phase0-validation-plan.md`. All 5 boxes green.
- **Recording corpus MUST be diverse.** 10 audios minimum: booking / FAQ / phone entry / correction / interrupt / urgent / spelling / code-switch (English↔Urdu) / hostile-caller / silence-then-speech. See `docs/soak/fixtures-README.md` for the format.
- **Same corpus, same tester, same day** — all 4 pipelines exercised in one benchmark window so provider variance doesn't dominate.
- **Cost tracking is mandatory.** Every pipeline reports $ per turn AND $ per full call. Doc #56 line 194 called cost as a first-class metric.
- **Time-box path C to 1 week.** Doc #56 line 106: if OpenAI Realtime scaffolding isn't working in a week, drop it, race A/B/D only.
- **Per-turn output schema is FIXED across paths.** Divergence in metric shape breaks the aggregator. See §Metric schema.
- **No R6/R7/PHASE2 branch merges during this phase.** We benchmark against a stable base — additions come after.

---

## The four pipelines

| ID | STT | Brain | TTS | Notable |
|---|---|---|---|---|
| **A** | Deepgram Nova-3 batch | Router LLM (OpenAI/Mistral) via HTTP | ElevenLabs Flash HTTP | Current stack — the baseline |
| **B** | Deepgram Flux (EagerEndOfTurn) | Router LLM via HTTP | ElevenLabs Multi-Context WS (persistent) | Streaming everywhere |
| **C** | OpenAI Realtime API (audio in) | OpenAI Realtime (text out) | ElevenLabs Flash HTTP | Realtime for perception + brain, keep ElevenLabs voice |
| **D** | Local Whisper on rig | Local vLLM on rig (Llama 3.3 70B) | ElevenLabs Flash HTTP | Local-heavy hybrid |

Notes:
- A is fully covered by the current codebase. Setting up A is verifying we can replay a WAV and get end-to-end metrics.
- B requires the Flux streaming path (feature flag `deepgram_use_flux=true` — already exists) AND the ElevenLabs Multi-Context WS wrapper to actually be persistent (doc #56 Addendum ratifies R2 finding that current WS is 5x SLOWER than HTTP; MUST re-verify claim before B ships).
- C requires new provider wiring under `apps/api/app/providers/llm/openai_realtime.py` (does not exist).
- D requires the 2×3090 rig reachable from the laptop (probably yes on LAN; if the rig has to be reachable when laptop moves, PHASE1 has a network dep).

---

## File Structure

- `apps/api/scripts/latency-lab/`  — new subdir for the harness
  - `run-pipeline.py`  — top-level runner: `--pipeline A|B|C|D --corpus DIR --out reports/`
  - `pipelines/`
    - `__init__.py`
    - `pipeline_a.py`  — current stack shim
    - `pipeline_b.py`  — Flux + persistent WS shim
    - `pipeline_c.py`  — OpenAI Realtime shim
    - `pipeline_d.py`  — local rig shim (calls the rig's HTTP API)
  - `metrics.py`  — LabMetrics dataclass + JSON schema + writer
  - `aggregate.py`  — reads N reports/*.jsonl, emits a Markdown comparison table
  - `README.md`  — how to run the whole thing
- `apps/api/data/latency_lab_corpus/*.wav`  — the 10-audio corpus (gitignored, per fixtures-README convention)
- `apps/api/data/latency_lab_reports/*.jsonl`  — per-turn metric output (gitignored)
- `docs/rnd-2026-08/60-realtime-cost-benchmark.md`  — written during G4 (Phase 0); consumed here
- `docs/rnd-2026-08/61-latency-lab-results.md`  — the final comparison table + winner declaration

---

## Task 1: Metric schema + writer

**Files:**
- Create: `apps/api/scripts/latency-lab/metrics.py`
- Test: `apps/api/tests/test_latency_lab_metrics.py`

**Interfaces:**
- Produces: `LabMetrics` dataclass with `to_jsonl()` method
  - Fields: `pipeline_id`, `wav_name`, `turn_index`, `stt_first_partial_ms`, `stt_final_ms`, `brain_start_ms`, `brain_first_token_ms`, `brain_done_ms`, `tts_first_byte_ms`, `wire_first_frame_ms`, `end_to_end_ms`, `tool_calls_made`, `tool_calls_correct`, `digits_captured_correct`, `cost_usd`, `provider_labels: dict`, `notes: str`

- [ ] **Step 1: Write the failing test**

```python
from apps.api.scripts.latency_lab.metrics import LabMetrics

def test_labmetrics_roundtrips_jsonl():
    m = LabMetrics(
        pipeline_id="A", wav_name="booking-01.wav", turn_index=0,
        stt_first_partial_ms=120, stt_final_ms=560, brain_start_ms=580,
        brain_first_token_ms=780, brain_done_ms=1240,
        tts_first_byte_ms=1290, wire_first_frame_ms=1450,
        end_to_end_ms=1450,
        tool_calls_made=1, tool_calls_correct=1,
        digits_captured_correct=None,
        cost_usd=0.0031,
        provider_labels={"stt": "deepgram-nova-3", "llm": "gpt-5.4-nano", "tts": "elevenlabs-flash"},
        notes="",
    )
    line = m.to_jsonl()
    round_tripped = LabMetrics.from_jsonl(line)
    assert round_tripped == m
```

- [ ] **Step 2: Run test to verify FAIL**  `pytest tests/test_latency_lab_metrics.py -v` → module not found

- [ ] **Step 3: Implement metrics.py**

```python
from __future__ import annotations
import json
from dataclasses import dataclass, asdict, field
from typing import Optional

@dataclass
class LabMetrics:
    pipeline_id: str
    wav_name: str
    turn_index: int
    stt_first_partial_ms: Optional[int] = None
    stt_final_ms: Optional[int] = None
    brain_start_ms: Optional[int] = None
    brain_first_token_ms: Optional[int] = None
    brain_done_ms: Optional[int] = None
    tts_first_byte_ms: Optional[int] = None
    wire_first_frame_ms: Optional[int] = None
    end_to_end_ms: Optional[int] = None
    tool_calls_made: int = 0
    tool_calls_correct: int = 0
    digits_captured_correct: Optional[bool] = None
    cost_usd: float = 0.0
    provider_labels: dict = field(default_factory=dict)
    notes: str = ""

    def to_jsonl(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_jsonl(cls, line: str) -> "LabMetrics":
        d = json.loads(line)
        return cls(**d)
```

- [ ] **Step 4: Run test to verify PASS**
- [ ] **Step 5: Commit** — `git commit -m "phase1: LabMetrics schema + jsonl round-trip"`

## Task 2: Pipeline A shim (current stack baseline)

**Files:**
- Create: `apps/api/scripts/latency-lab/pipelines/pipeline_a.py`
- Test: `apps/api/tests/test_latency_lab_pipeline_a.py`

**Interfaces:**
- Consumes: WAV path, output metrics writer
- Produces: `async def run(wav_path: pathlib.Path, writer) -> None` — replays WAV through the existing actor via `handle_twilio_stream_via_actor`; captures the same turn spans the actor already emits; writes one `LabMetrics` per turn.

**Approach:**
- Reuse `apps/api/scripts/replay-audio.py` logic (already on `chore/soak-harness`)
- Subscribe to the same event log the actor writes (`packages/observability/call_event_log.py`)
- Convert `TurnSpan` marks into `LabMetrics` fields

- [ ] **Step 1: Write the failing test** (against a tiny 2-second WAV in `apps/api/tests/fixtures/`)
- [ ] **Step 2: Verify FAIL**
- [ ] **Step 3: Implement (~150 lines)**
- [ ] **Step 4: Verify PASS**
- [ ] **Step 5: Commit**

## Task 3: Recording corpus

**Files:**
- Create: `apps/api/data/latency_lab_corpus/*.wav` (10 files)
- Create: `apps/api/data/latency_lab_corpus/README.md`  (transcript per file)

**Coverage:**
- 01-booking-simple.wav — "Book me tomorrow at 10 for a cleaning, phone 650-253-0000, name John"
- 02-booking-pk.wav — same but PK number spoken as "zero triple three, five two four four, seven seven two"
- 03-faq.wav — "What are your hours?"
- 04-phone-only.wav — just the phone dictation, mid-flow
- 05-correction.wav — "actually wait, no, make it 2pm"
- 06-interrupt.wav — caller cuts agent off mid-sentence
- 07-urgent.wav — "I have chest pain" (should escalate)
- 08-spelling.wav — "my name is J-O-N as in John — no like J-O-H-N"
- 09-code-switch.wav — English + Urdu ("kya aap English mein baat kar sakte hain?")
- 10-silence-then-speech.wav — 4s silence, then "hello?"

Recorded on real phone in µ-law 8kHz. Not checked in (per fixtures convention).

- [ ] **Step 1: Record all 10 WAVs** (manual — done outside code)
- [ ] **Step 2: Write the README with transcripts**
- [ ] **Step 3: Verify each WAV plays correctly** with `afplay` (Mac) or `aplay` (Linux)
- [ ] **Step 4: Verify replay-audio.py can send each one** end-to-end against the actor
- [ ] **Step 5: Commit README** (WAVs remain gitignored)

## Task 4: Pipeline B — Flux + ElevenLabs Multi-Context WS

**Files:**
- Create: `apps/api/scripts/latency-lab/pipelines/pipeline_b.py`
- Modify: `apps/api/app/providers/tts/elevenlabs_tts.py` (add explicit "persistent" mode if not already there)
- Verify: doc #56 Addendum re: ws_stream_synthesize being 5x SLOWER for full-text sends

**Approach:**
1. Flip `deepgram_use_flux=true` in a per-run env (do not commit)
2. Configure ElevenLabs to keep the WS open across turns
3. Same replay-and-measure loop as pipeline A
4. Compare metrics

**Risks:**
- **Doc #56 finding** (`elevenlabs-ws-bench-finding.md` memory): current WS wrapper opens fresh WS per synth. Re-verify BEFORE running B. If still true, either fix the wrapper first or run B in "HTTP TTS" fallback and mark the WS piece as "future work."
- Flux only supports English + 10 langs; Urdu / code-switch WAVs (09) SKIP pipeline B — mark in metrics.

- [ ] **Step 1: Re-verify ws_stream_synthesize behavior** — write a 30-line reproducer, confirm memory finding
- [ ] **Step 2: Decide: fix wrapper or fall back to HTTP TTS**
- [ ] **Step 3: Write test for pipeline_b.py**
- [ ] **Step 4: Implement**
- [ ] **Step 5: Verify PASS on corpus 01-08 (skip 09-10 if applicable)**
- [ ] **Step 6: Commit**

## Task 5: Pipeline C — OpenAI Realtime (audio in, text out)

**Files:**
- Create: `apps/api/app/providers/llm/openai_realtime.py`
- Create: `apps/api/scripts/latency-lab/pipelines/pipeline_c.py`
- Consumes: `docs/rnd-2026-08/60-realtime-cost-benchmark.md` (from Phase 0 G4) — if cost is prohibitive, SKIP this whole task

**Approach:**
1. Verify Realtime API supports `output_modalities: ["text"]` in 2026 (docs may have changed since doc #56 line 23)
2. Build a thin provider that streams µ-law 8kHz IN, streams text OUT
3. Pipe text output into existing ElevenLabs TTS (which we already trust)
4. Bookkeep audio-minutes for cost

**Time-box:** 1 week per doc #56 line 106. If not working after 1 week, drop pipeline C, race A/B/D only.

- [ ] **Step 1: Verify text-only output modality is still supported** (curl the Realtime API, confirm)
- [ ] **Step 2: If unsupported → SKIP TASK 5, document the finding**
- [ ] **Step 3: Write test for openai_realtime.py**
- [ ] **Step 4: Implement provider (~200 lines)**
- [ ] **Step 5: Write test for pipeline_c.py**
- [ ] **Step 6: Implement pipeline shim**
- [ ] **Step 7: Verify PASS on corpus 01-08**
- [ ] **Step 8: Commit**

## Task 6: Pipeline D — Local Whisper + local Llama on 2×3090

**Files:**
- Create: `apps/api/app/providers/stt/local_whisper_client.py`
- Create: `apps/api/app/providers/llm/local_vllm_client.py`
- Create: `apps/api/scripts/latency-lab/pipelines/pipeline_d.py`
- Modify: `apps/api/app/core/config.py` — add `local_rig_stt_url`, `local_rig_llm_url`

**Approach:**
1. Rig hosts Whisper via `whisper.cpp` HTTP server (or faster-whisper server)
2. Rig hosts vLLM serving Llama 3.3 70B on both cards
3. Laptop calls both over LAN
4. Same replay-and-measure loop

**Risks:**
- Rig must be reachable from wherever benchmark runs. If laptop moves off LAN, D is skipped for that run.
- vLLM tool-calling support varies by model — verify Llama 3.3 70B works with tools before benchmarking.

- [ ] **Step 1: Provision rig** — Whisper server + vLLM up, curl-testable
- [ ] **Step 2: Write STT client + test**
- [ ] **Step 3: Write LLM client + test (with tool calls)**
- [ ] **Step 4: Write pipeline_d.py + test**
- [ ] **Step 5: Verify PASS on full corpus**
- [ ] **Step 6: Commit**

## Task 7: Aggregation report

**Files:**
- Create: `apps/api/scripts/latency-lab/aggregate.py`
- Create: `docs/rnd-2026-08/61-latency-lab-results.md`

**Interfaces:**
- Consumes: `apps/api/data/latency_lab_reports/*.jsonl` (one file per pipeline × wav)
- Produces: a Markdown table:

```
| Pipeline | Corpus size | p50 E2E | p95 E2E | tool-acc | digit-acc | cost/turn | cost/call | notes |
|---|---|---|---|---|---|---|---|---|
| A | 10/10 | 1420ms | 2340ms | 8/10 | 4/5 | $0.003 | $0.021 | baseline |
| B | 9/10 | ??? | ??? | ??? | ??? | ??? | ??? | skipped 09 (Urdu) |
| C | 8/10 | ??? | ??? | ??? | ??? | ??? | ??? | skipped 09-10 (Realtime lang) |
| D | 10/10 | ??? | ??? | ??? | ??? | $0.00 | $0.00 | local; own hw amortized |
```

Plus a "winner" declaration with rationale.

- [ ] **Step 1: Write aggregate.py** (~100 lines, pure stdlib)
- [ ] **Step 2: Write test with 3 synthetic jsonl files**
- [ ] **Step 3: Verify PASS**
- [ ] **Step 4: Run against real data (after Tasks 2/4/5/6 have produced data)**
- [ ] **Step 5: Write `61-latency-lab-results.md` with the table + declared winner**
- [ ] **Step 6: Commit**

## Task 8: PHASE2 handoff writeup

**Files:**
- Modify: `docs/rnd-2026-08/58-status-and-phase-map-2026-08-14.md` — add "PHASE1 winner: <ID>" and cite `61-latency-lab-results.md`
- Modify: `docs/rnd-2026-08/56-phase-change-my-analysis.md` — Addendum E: PHASE1 findings
- Create: `docs/superpowers/plans/2026-08-14-phase2-2a-shadow.md` gets a PHASE1 winner section added (see `2a` plan)

- [ ] **Step 1: Update 58 with winner + date**
- [ ] **Step 2: Write Addendum E**
- [ ] **Step 3: Cross-link from PHASE2 plans**
- [ ] **Step 4: Commit** — this is the PHASE1 close-out commit

---

## Success criteria

PHASE1 exits when:
- The comparison table exists at `docs/rnd-2026-08/61-latency-lab-results.md` with real numbers for at least A, D, and one of B or C
- The winning pipeline is named
- A short rationale (3-5 sentences) explains why the winner beats the runners-up on the combination of latency, cost, and accuracy
- Doc #58 is updated to reflect the winner
- No production code has changed (this is a benchmarking phase — winner shipping is PHASE2's job)

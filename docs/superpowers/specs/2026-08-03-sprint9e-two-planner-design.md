# Sprint 9e — Two-planner LLM Design

**Status:** approved for implementation
**Date:** 2026-08-03
**Author:** claude
**Superpowers workflow:** brainstorming → this spec → writing-plans → executing-plans

## Goal

Split the receptionist brain into two planners so agent delivery matches
speech act — warm on greetings, reassuring on bad news, neutral on slot
listings — instead of a single one-size voice for every turn.

## Non-goals (Sprint 9e)

- Mid-utterance delivery swap (needs chunked TTS from Sprint 10).
- `context.prior_spoken_text` continuity (defer, needs ledger.heard_text_for wiring).
- Cartesia integration (Sprint 9d compiler exists, but 9e wires ElevenLabs first).
- Voice DNA reference-bank retrieval (Sprint 10+ per moat doc).

## Architecture

```
User utterance
  │
  ▼
┌─────────────────────────────────────────────┐
│ Semantic planner (packages/.../semantic.py) │
│  wraps existing ReceptionistBrain           │
│  returns SemanticOutput:                    │
│    text: str                                │
│    speech_act: SpeechAct  (default NEUTRAL) │
│    tool_calls: list                         │
└─────────────────────────────────────────────┘
  │  if tool_calls → run tools → loop
  │
  ▼
FORK (asyncio.create_task, no barrier):
  │
  ├── Performance planner (packages/.../performance.py)
  │     Groq llama-3.1-8b-instant
  │     Input:  text + speech_act + business context
  │     Output: Delivery block
  │     On error/timeout: return default_delivery_for(speech_act)
  │     Metric bump: two_planner_delivery_hit_rate
  │
  ▼
Build VPLUtterance(text, speech_act, delivery=<from perf OR default>)
Validate + repair (Sprint 9c validator)
  │
  ▼
Compile to provider payload (Sprint 9d compiler)
  │
  ▼
Provider.synthesize_from_plan(compiled_plan) → audio bytes
  │
  ▼
CallActor ledger → Twilio mulaw stream
```

**Critical design decision — parallel not sequential.** The performance
planner runs concurrently with the compile+synth path. If it returns
before TTS finishes: use its Delivery. If it doesn't: default Delivery
was already used. No blocking wait. This keeps end-of-turn latency at
1× LLM cost, not 2×.

**In Sprint 9e**, the parallel fork degenerates to "wait up to 200ms for
perf, then proceed with whatever we have." The full parallel-with-swap
lands when TTS chunking exists (Sprint 10). This is documented as
`Sprint 10 refinement` in the code.

## Components

### 1. `packages/core_agent/planners/__init__.py`

New sub-package. Exposes:
- `SemanticOutput` — dataclass: `text, speech_act, tool_calls`
- `SemanticPlanner` — wraps existing `ReceptionistBrain.run_user_turn`
- `PerformancePlanner` — the Groq 8B call

### 2. `packages/core_agent/planners/semantic.py`

Thin wrapper. Does not rewrite the brain.

```python
class SemanticPlanner:
    def __init__(self, brain: ReceptionistBrain): ...

    async def plan(self, state: CallState, user_text: str) -> SemanticOutput:
        # Delegates to brain.run_user_turn.
        # Extracts speech_act from the brain's JSON output.
        # If speech_act is missing/invalid: log warn, return NEUTRAL.
```

Return type includes `tool_calls` so the actor loop can decide to keep
looping (existing behavior).

### 3. `packages/core_agent/prompt.py`

Append a **short** section instructing the brain to emit `speech_act`
alongside `reply` in its structured output. Full enum listed with one
example each. Instruction: "if unsure, use NEUTRAL". This constraint
prevents drift on the primary reasoning (booking correctness).

Prompt token cost estimate: ~180 tokens added. Acceptable — Groq 70B
per-turn cost is dominated by the response, not the prompt.

### 4. `packages/core_agent/planners/performance.py`

```python
@dataclass(frozen=True)
class PerformancePlan:
    delivery: Delivery
    used_fallback: bool
    latency_ms: int
    error: str | None

class PerformancePlanner:
    def __init__(
        self,
        llm: LLM,                       # injected — usually a Groq client
        timeout_ms: int = 200,
        model: str = "llama-3.1-8b-instant",
    ): ...

    async def plan(
        self,
        text: str,
        speech_act: SpeechAct,
        business_name: str,
    ) -> PerformancePlan:
        # Build tight structured-output prompt with speech_act as hint
        # asyncio.wait_for(..., timeout=self.timeout_ms/1000)
        # Parse response into Delivery; validate; on any exception →
        # PerformancePlan(delivery=default_delivery_for(speech_act),
        #                 used_fallback=True, error=str(e))
```

**Prompt (draft, ~120 tokens):**
```
You are a voice delivery planner. Given text the agent is about to say
and its speech_act, return a JSON delivery spec:

{
  "style": "warm|reassuring|urgent|apologetic|professional|neutral",
  "intensity": 0.0-1.0,
  "rate": 0.6-1.4,
  "pause_before_ms": 0-1500,
  "pause_after_ms": 0-1500
}

speech_act={speech_act}, text="{text}"
Respond with ONLY the JSON.
```

Response validated by `validate_vpl_and_repair` — repairs illegal
values silently, only unrepairable errors fall through to default.

### 5. `apps/api/app/providers/tts/elevenlabs_tts.py`

Add new method, keep old one:

```python
async def synthesize_from_plan(
    self, plan: CompiledSpeechPlan,
) -> tuple[bytes, str]:
    """Sprint 9e: takes a compiled VPL plan instead of raw text.
    Uses plan.request_payload as-is (already provider-shaped by
    packages/voice/vpl/compilers/elevenlabs.compile_elevenlabs).
    """
```

Old `synthesize(text)` stays untouched for backward compat + non-actor
paths (browser widget, greeting cache).

### 6. `apps/api/app/routes/twilio_actor.py`

Replace `_stream_tts(text, gen)`. New flow:

```python
async def _stream_tts(self, semantic: SemanticOutput, gen: int) -> None:
    span = self._current_turn_span

    # Kick off perf planner in parallel — up to 200ms
    perf_task = asyncio.create_task(
        self._perf_planner.plan(
            semantic.text, semantic.speech_act, self.business_name,
        )
    )
    try:
        perf_plan = await asyncio.wait_for(perf_task, timeout=0.2)
        delivery = perf_plan.delivery
        _tel.record_two_planner_hit(self.tenant_id, hit=not perf_plan.used_fallback)
    except asyncio.TimeoutError:
        perf_task.cancel()
        delivery = default_delivery_for(semantic.speech_act)
        _tel.record_two_planner_hit(self.tenant_id, hit=False)

    utt = VPLUtterance(
        text=semantic.text,
        speech_act=semantic.speech_act,
        delivery=delivery,
    )
    utt, _ = validate_vpl_and_repair(utt)
    plan = compile_elevenlabs(
        utt, voice_id=self.voice_id, output_format="ulaw_8000",
    )
    audio, mime = await self._tts.synthesize_from_plan(plan)
    ...  # ledger + framing unchanged
```

### 7. `packages/runtime/telemetry.py`

```python
TWO_PLANNER_HIT = Counter(
    "voiceops_two_planner_hit_total",
    "Perf planner returned in time.",
    ["tenant_id", "hit"],  # hit in {"true","false"}
)

def record_two_planner_hit(tenant_id: str, hit: bool) -> None: ...
```

Alert (out of scope for Sprint 9e — noted for the observability plan):
if hit-rate < 0.5 over 100 turns per tenant, log WARN.

## Data flow

| Step | Input | Output |
|---|---|---|
| 1 | user utterance (audio) | text (STT) |
| 2 | text + state | `SemanticOutput{text, speech_act, tool_calls}` |
| 3a | text + speech_act | `PerformancePlan{delivery, used_fallback}` |
| 3b | speech_act (fallback path) | `default_delivery_for(speech_act)` |
| 4 | text + speech_act + delivery | `VPLUtterance` |
| 5 | VPLUtterance | `CompiledSpeechPlan` (provider payload) |
| 6 | CompiledSpeechPlan | audio bytes + mime |
| 7 | audio | Twilio mulaw frames + ledger marks |

## Error handling

Every layer degrades to the layer below:

| Layer | Failure | Fallback |
|---|---|---|
| Semantic (existing brain) | LLM error | existing brain's fallback text |
| Semantic speech_act parse | invalid enum | `SpeechAct.NEUTRAL` |
| Performance planner LLM | error, timeout | `default_delivery_for(speech_act)` |
| Performance planner JSON parse | malformed | same fallback |
| VPL validator | unrepairable | log ERROR, use `Delivery()` (Pydantic defaults) |
| Compiler | exception | log ERROR, drop to legacy `synthesize(text)` path |
| Provider | exception | existing telephony error path |

Every fallback is silent (metric-visible only). The compile→legacy
fallback in row 6 is the emergency brake: worst case, we ship the
current-of-main behavior.

## Testing (Sprint 9e scope)

Tests land as part of implementation, not deferred to QA sprint:

- `tests/test_semantic_planner.py`
  - speech_act parsed from brain output
  - malformed speech_act → NEUTRAL fallback
  - tool_calls loop preserved
- `tests/test_performance_planner.py`
  - success path returns Delivery
  - timeout returns fallback with `used_fallback=True`
  - LLM error returns fallback
  - malformed JSON returns fallback
  - respects `safety.maximum_emotional_intensity` via validator repair
- `tests/test_twilio_actor.py` (extend)
  - full turn through both planners (fakes for LLM + TTS)
  - `voiceops_two_planner_hit_total` bumps correctly
- Integration test (mocked LLMs, real VPL compiler + validator)
  - warm greeting produces warm-style ElevenLabs payload
  - emergency utterance produces low-intensity payload

## Metrics

- `voiceops_two_planner_hit_total{tenant_id, hit}` — new counter
- `voiceops_llm_router_hits_total{provider}` — existing, will now
  include per-planner hits (add `planner` label in a followup)

## Risks

1. **Groq rate limits.** 8B model is 14K RPD free tier. At 5 turns/call
   that's 2800 calls/day. Fine for demo, needs paid tier at ~100 calls/day.
2. **Semantic prompt drift.** Adding speech_act might degrade booking
   accuracy. Mitigation: keep the taxonomy at the *end* of the prompt,
   after the receptionist persona; run adversarial rerun (Sprint 10 QA).
3. **Latency tail.** The 200ms cap can still tail into 300ms if Groq is
   slow. Mitigation: parallel fork means we're not blocking synth.

## Rollback

Feature flag: `TWO_PLANNER_ENABLED=false` in `apps/api/app/core/config.py`.
When false, `_stream_tts` uses the current path (single planner, no VPL
compilation). Zero-risk rollback.

## Open questions (deferred)

- Should the performance planner get streaming access to the semantic
  planner's tokens (start planning before semantic finishes)? — needs
  streaming LLM interface, defer.
- Should default_delivery_for be tenant-configurable? — yes eventually,
  needs voice_profile table (Sprint 10+).

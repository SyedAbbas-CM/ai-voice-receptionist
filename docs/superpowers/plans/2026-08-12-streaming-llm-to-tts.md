# Streaming LLM → TTS Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut perceived-response latency ~600-800ms by piping LLM tokens straight into TTS as sentences complete, instead of waiting for the full reply.

**Architecture:** Thread an optional `on_delta(str)` callback from `_run_brain` through `session_manager.run_user_turn` → `brain.handle_user_turn` → the FINAL post-tool-loop `llm.stream_complete()` call. The actor's callback buffers tokens, splits at sentence boundaries, and starts speaking each sentence the moment it's complete. The last sentence still triggers the fake-booking guard against the full accumulated text; if the guard trips, we cancel remaining audio and speak the safe fallback instead. Feature-flagged (`STREAMING_LLM_TO_TTS=false` default). Gated to no-tools/no-VPL/no-browser/mistral-only turns.

**Tech Stack:** Python asyncio, httpx SSE (Mistral `stream_complete`), ElevenLabs HTTP `stream_synthesize` (Phase 1) → ElevenLabs WS `ws_stream_synthesize` incremental push (Phase 2).

## Global Constraints

- Feature flag `settings.streaming_llm_to_tts: bool = False` gates everything. Off by default. User will flip on for testing.
- Gate the streaming path OFF when ANY of:
  - `settings.two_planner_enabled` (VPL path consumes full text)
  - `self.stream_sid.startswith("browser_")` (browser leg — Phase 1 covers phone only)
  - `not hasattr(brain.llm, "stream_complete")` (router-resolved provider isn't Mistral)
  - Any tool_calls in the resolved brain response (tool turns aren't streamed — safety)
- Fake-booking guard (`_reply_lies_about_booking` in `packages/core_agent/brain.py:396`) MUST run on the full accumulated text before audio is committed as "final". If it trips, cancel the in-flight TTS, speak the safe fallback, and log a warning.
- Sanitizer (`sanitize_for_speech`) must apply per-sentence, not per-token — token boundaries can split abbreviations mid-word.
- Ledger `full_text` must still be populated for barge-in reconciliation (`_reconcile_transcript_on_interrupt`). Populate from the accumulated buffer at end-of-stream.
- Speech-act inference (`_infer_speech_act_from_payload` at `apps/api/app/routes/twilio_actor.py:933`) must still receive the full payload. Compute AFTER streaming completes; don't tie it to first-sentence dispatch.
- Provider first-token telemetry: log `LLM_STREAM_START`, `LLM_FIRST_TOKEN`, `LLM_STREAM_DONE` with call_id, gen, provider, model, elapsed_ms. Log `TTS_SENTENCE_QUEUED` per sentence.
- No changes to `browser_widget` audio path in Phase 1.
- No router LLM streaming in this plan. If Mistral fails / is unavailable, batch fallback path must handle it invisibly.
- Every task ends with a commit. No `--no-verify`. No amending previous commits.

---

## File Structure

**New files:**
- `apps/api/tests/test_streaming_llm_pipeline.py` — unit tests for sentence buffer + fake-booking guard interplay
- `packages/core_agent/streaming.py` — the `SentenceBuffer` helper class (single responsibility: token stream → sentence stream + full-text accumulator)

**Modified files:**
- `apps/api/app/core/config.py` — add `streaming_llm_to_tts: bool = False`
- `apps/api/app/providers/base.py` — extend `LLMProvider` ABC with optional `stream_complete()` signature (documented, not abstract — providers opt in)
- `packages/core_agent/brain.py:201-540` — extend `handle_user_turn` with optional `on_delta` kwarg; wire to `stream_complete` on the final post-tool-loop path
- `apps/api/app/core/session_manager.py:317-334` — pass `on_delta` through
- `apps/api/app/routes/twilio_actor.py:895-940` — new `_run_brain_streaming` path; `_stream_sentence_to_tts` helper; wiring in `_run_brain`

**Reference files (read-only — do NOT modify):**
- `apps/api/app/providers/llm/mistral_llm.py:100-153` — the existing `stream_complete` shape we're consuming
- `apps/api/app/providers/tts/elevenlabs_tts.py:88-120` — HTTP `stream_synthesize` and `ws_stream_synthesize:122`
- `packages/voice/sentence_splitter.py:167` — `split_into_speakable_chunks(text) -> list[str]`
- `packages/voice/speech_sanitizer.py` — `sanitize_for_speech(text) -> str`

---

## Task 1: SentenceBuffer helper + config flag (foundation)

**Files:**
- Create: `packages/core_agent/streaming.py`
- Create: `apps/api/tests/test_streaming_llm_pipeline.py`
- Modify: `apps/api/app/core/config.py` (add flag after existing `elevenlabs_use_ws`)

**Interfaces:**
- Consumes: nothing
- Produces:
  ```python
  # packages/core_agent/streaming.py
  class SentenceBuffer:
      def __init__(self, min_first_chars: int = 20) -> None: ...
      def push(self, delta: str) -> list[str]:
          """Append tokens. Returns a list of newly-complete sentences
          (empty if no boundary yet). First-sentence emit is gated by
          min_first_chars so we don't fire on 'Yes.'."""
      def flush(self) -> str:
          """Return the residual buffer (whatever hasn't hit a boundary
          yet). Call at end-of-stream. Returned text may be empty."""
      @property
      def full_text(self) -> str:
          """Everything pushed so far, unmodified. For guards + ledger."""
  ```

- [ ] **Step 1: Write the failing test**

```python
# apps/api/tests/test_streaming_llm_pipeline.py
from packages.core_agent.streaming import SentenceBuffer


def test_sentence_buffer_yields_on_period():
    buf = SentenceBuffer(min_first_chars=5)
    assert buf.push("Hello there") == []
    assert buf.push(", how can I help") == []
    out = buf.push(" you today? Next")
    assert out == ["Hello there, how can I help you today?"]
    assert buf.full_text == "Hello there, how can I help you today? Next"


def test_sentence_buffer_min_first_chars_blocks_tiny_first_sentence():
    buf = SentenceBuffer(min_first_chars=20)
    assert buf.push("Sure. Let me check that for you.") == [
        "Sure. Let me check that for you.",
    ]


def test_sentence_buffer_min_first_chars_only_blocks_first():
    buf = SentenceBuffer(min_first_chars=20)
    out = buf.push("Sure, one moment. Yes.")
    assert out == ["Sure, one moment.", "Yes."]


def test_sentence_buffer_flush_returns_residual():
    buf = SentenceBuffer(min_first_chars=5)
    buf.push("First sentence. Trailing without period")
    assert buf.flush() == "Trailing without period"


def test_sentence_buffer_empty_stream():
    buf = SentenceBuffer()
    assert buf.flush() == ""
    assert buf.full_text == ""


def test_sentence_buffer_handles_question_and_exclaim():
    buf = SentenceBuffer(min_first_chars=5)
    out = buf.push("Are you sure? Yes! And no.")
    assert out == ["Are you sure?", "Yes!", "And no."]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd "/Users/az/Desktop/Receptionist Agent" && python -m pytest apps/api/tests/test_streaming_llm_pipeline.py -v`
Expected: FAIL with `ModuleNotFoundError: packages.core_agent.streaming`

- [ ] **Step 3: Write minimal implementation**

```python
# packages/core_agent/streaming.py
from __future__ import annotations

import re

# End-of-sentence: . ? ! followed by whitespace or end-of-string.
# Avoid splitting on abbreviations by requiring the char to NOT be
# preceded by a single capital letter (e.g. "Dr."). Cheap heuristic;
# the sanitizer expands abbreviations later so any leaks are cosmetic.
_SENT_END = re.compile(r'(?<![A-Z])[.?!](?:\s+|$)')


class SentenceBuffer:
    """Accumulates streamed LLM tokens and emits complete sentences.

    The buffer holds tokens until a sentence-ending punctuation lands
    followed by whitespace or stream end. `push()` returns any newly
    complete sentences; `flush()` returns whatever is left after the
    stream ends. `full_text` is always the raw accumulated stream —
    used for guards (fake-booking) and ledger reconciliation.

    `min_first_chars` prevents firing on a lone "Yes." or "Sure." at
    the start of the reply — those are too short to justify a TTS RTT.
    """

    def __init__(self, min_first_chars: int = 20) -> None:
        self._buf = ""
        self._full = ""
        self._first_emitted = False
        self._min_first_chars = min_first_chars

    def push(self, delta: str) -> list[str]:
        if not delta:
            return []
        self._full += delta
        self._buf += delta
        out: list[str] = []
        while True:
            m = _SENT_END.search(self._buf)
            if m is None:
                break
            end = m.end()
            candidate = self._buf[:end].strip()
            if not candidate:
                self._buf = self._buf[end:]
                continue
            if not self._first_emitted and len(candidate) < self._min_first_chars:
                # Wait for more text before emitting the first sentence.
                # Don't consume the buffer yet — leave it for the next push.
                break
            out.append(candidate)
            self._buf = self._buf[end:]
            self._first_emitted = True
        return out

    def flush(self) -> str:
        residual = self._buf.strip()
        self._buf = ""
        return residual

    @property
    def full_text(self) -> str:
        return self._full
```

- [ ] **Step 4: Add the config flag**

Edit `apps/api/app/core/config.py`. Find the `elevenlabs_use_ws` line (added 2026-08-12) and append after it:

```python
    # 2026-08-12 (task #283): Streaming LLM tokens directly into TTS
    # per sentence. First sentence audio arrives ~250ms after brain
    # fires instead of ~1000ms for full reply. Off by default while
    # the fake-booking guard interplay is being validated. Gated to
    # no-tools, no-VPL, no-browser, Mistral-only turns.
    streaming_llm_to_tts: bool = False
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd "/Users/az/Desktop/Receptionist Agent" && python -m pytest apps/api/tests/test_streaming_llm_pipeline.py -v`
Expected: 6 passed

- [ ] **Step 6: Verify config imports cleanly**

Run: `cd "/Users/az/Desktop/Receptionist Agent/apps/api" && python -c "from app.core.config import settings; print('streaming_llm_to_tts=', settings.streaming_llm_to_tts)"`
Expected: `streaming_llm_to_tts= False`

- [ ] **Step 7: Commit**

```bash
git add packages/core_agent/streaming.py apps/api/tests/test_streaming_llm_pipeline.py apps/api/app/core/config.py
git commit -m "$(cat <<'EOF'
feat(task-283): SentenceBuffer + streaming_llm_to_tts flag

Foundation for streaming LLM→TTS: buffer that splits token deltas
into sentences with a min-first-chars gate to skip tiny openers.
Feature flag off by default.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Extend LLMProvider ABC with optional stream_complete

**Files:**
- Modify: `apps/api/app/providers/base.py:47-55`
- Test: `apps/api/tests/test_streaming_llm_pipeline.py` (extend)

**Interfaces:**
- Consumes: nothing new; documents the existing `stream_complete` shape from `mistral_llm.py:100`
- Produces:
  ```python
  # LLMProvider base — documented optional method
  async def stream_complete(
      self,
      messages: list[dict],
      temperature: float = 0.3,
      max_tokens: int = 1024,
  ) -> AsyncIterator[tuple[str, bool]]:
      """Yields (delta_text, is_final). Optional — providers opt in.
      Default raises NotImplementedError so callers can hasattr-check."""
  ```

- [ ] **Step 1: Write the failing test**

Append to `apps/api/tests/test_streaming_llm_pipeline.py`:

```python
import pytest
from app.providers.base import LLMProvider
from app.providers.llm.mistral_llm import MistralLLM


class _DummyLLM(LLMProvider):
    name = "dummy"

    async def complete(self, messages, tools=None, temperature=0.3,
                       max_tokens=1024, response_schema=None):
        raise NotImplementedError


def test_llm_base_stream_complete_raises_by_default():
    llm = _DummyLLM()
    with pytest.raises(NotImplementedError):
        # An async-gen: iterating triggers the body.
        agen = llm.stream_complete([{"role": "user", "content": "hi"}])
        # Force at least one step so the raise fires
        import asyncio
        asyncio.get_event_loop().run_until_complete(agen.__anext__())


def test_mistral_still_has_stream_complete():
    # Regression guard — task 283 depends on this method's existence
    assert hasattr(MistralLLM, "stream_complete")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd "/Users/az/Desktop/Receptionist Agent" && python -m pytest apps/api/tests/test_streaming_llm_pipeline.py::test_llm_base_stream_complete_raises_by_default -v`
Expected: FAIL with `AttributeError` or `TypeError` (base doesn't declare `stream_complete`)

- [ ] **Step 3: Add stream_complete to LLMProvider base**

Edit `apps/api/app/providers/base.py`. After the existing `stream` method (line ~55) add:

```python
    async def stream_complete(
        self,
        messages: list[dict],
        temperature: float = 0.3,
        max_tokens: int = 1024,
    ):
        """Optional token-level streaming for the FINAL post-tool-loop
        reply. Yields (delta_text: str, is_final: bool) tuples.

        Providers opt in by overriding. The default raises so callers
        can `hasattr(llm, 'stream_complete')` AND still handle a router
        LLM whose current pick doesn't support it (the router LLM itself
        overrides to try each candidate; if none support streaming, it
        re-raises NotImplementedError and the caller falls back to
        batch `complete()`).

        No tools / no response_schema — this path is ONLY for the final
        plain-text reply after the tool loop has resolved.
        """
        raise NotImplementedError(
            f"{self.name} does not support token-level streaming",
        )
        yield  # pragma: no cover — makes this an async generator
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd "/Users/az/Desktop/Receptionist Agent" && python -m pytest apps/api/tests/test_streaming_llm_pipeline.py -v`
Expected: 8 passed (6 from task 1 + 2 new)

- [ ] **Step 5: Commit**

```bash
git add apps/api/app/providers/base.py apps/api/tests/test_streaming_llm_pipeline.py
git commit -m "$(cat <<'EOF'
feat(task-283): document stream_complete on LLMProvider ABC

Optional method; providers opt in by overriding. Default raises
NotImplementedError so callers can hasattr-check and fall back to
batch complete() when the resolved provider doesn't stream.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Thread on_delta through brain + session_manager (plumbing only)

**Files:**
- Modify: `packages/core_agent/brain.py:201` (`handle_user_turn`) + `:308` (final `llm.complete` site)
- Modify: `apps/api/app/core/session_manager.py:317` (`run_user_turn`)
- Test: `apps/api/tests/test_streaming_llm_pipeline.py`

**Interfaces:**
- Consumes: `SentenceBuffer` from Task 1; `stream_complete()` shape from Task 2
- Produces:
  ```python
  # brain.py
  async def handle_user_turn(
      self, state: CallState, user_text: str,
      on_delta: Optional[Callable[[str], Awaitable[None]]] = None,
  ) -> BrainTurnResult: ...

  # session_manager.py
  async def run_user_turn(
      state: CallState, brain: ReceptionistBrain, user_text: str,
      on_delta: Optional[Callable[[str], Awaitable[None]]] = None,
  ) -> dict: ...
  ```

- [ ] **Step 1: Write the failing test**

Append to `apps/api/tests/test_streaming_llm_pipeline.py`:

```python
import asyncio
from unittest.mock import AsyncMock, MagicMock


def test_handle_user_turn_accepts_on_delta_kwarg():
    """Signature check only — deep integration tested via manual browser
    verification in Task 7. This guards accidental removal of the
    on_delta kwarg."""
    from packages.core_agent.brain import ReceptionistBrain
    import inspect
    sig = inspect.signature(ReceptionistBrain.handle_user_turn)
    assert "on_delta" in sig.parameters, (
        "handle_user_turn must accept on_delta kwarg for streaming"
    )
    # Must default to None so all existing callers keep working
    assert sig.parameters["on_delta"].default is None


def test_run_user_turn_accepts_on_delta_kwarg():
    from app.core.session_manager import run_user_turn
    import inspect
    sig = inspect.signature(run_user_turn)
    assert "on_delta" in sig.parameters
    assert sig.parameters["on_delta"].default is None


def test_handle_user_turn_invokes_on_delta_when_streaming(monkeypatch):
    """When the provider has stream_complete AND on_delta is passed AND
    no tools are called, brain should stream. Simulate with a stub LLM."""
    from packages.core_agent.brain import ReceptionistBrain
    from packages.core_agent.state import CallState
    from app.providers.base import LLMResponse

    class StubStreamLLM:
        name = "stub"
        model = "stub-1"
        async def complete(self, messages, tools=None, temperature=0.3,
                           max_tokens=1024, response_schema=None,
                           site=None):
            # After streaming path collects tokens, brain may call
            # complete again for structured metadata — return no
            # tool_calls to close the loop cleanly.
            return LLMResponse(text="Hello there. All good.", tool_calls=[])
        async def stream_complete(self, messages, temperature=0.3,
                                  max_tokens=1024):
            for tok in ["Hello ", "there. ", "All ", "good."]:
                yield tok, False
            yield "", True

    # Build a brain with the stub. Skip real system_prompt / tools loading.
    brain = ReceptionistBrain.__new__(ReceptionistBrain)
    brain.llm = StubStreamLLM()
    brain.system_prompt = "sys"
    brain.tools = []
    brain.rag = None
    brain._refresh_extraction_bg = lambda s: None  # no-op
    brain.MAX_TOOL_ITERATIONS = 4

    state = CallState(session_id="s1", business_id="b1", tenant_id="t1")

    received = []
    async def cb(delta: str):
        received.append(delta)

    result = asyncio.get_event_loop().run_until_complete(
        brain.handle_user_turn(state, "hi", on_delta=cb)
    )
    # The full reply text should be assembled from deltas
    assert result.reply.strip() == "Hello there. All good."
    # We should have received at least one delta (the concatenation of
    # tokens equals the full text)
    assert "".join(received) == "Hello there. All good."
```

- [ ] **Step 2: Run the signature tests to verify they fail**

Run: `cd "/Users/az/Desktop/Receptionist Agent" && python -m pytest apps/api/tests/test_streaming_llm_pipeline.py::test_handle_user_turn_accepts_on_delta_kwarg apps/api/tests/test_streaming_llm_pipeline.py::test_run_user_turn_accepts_on_delta_kwarg -v`
Expected: FAIL (kwarg not present)

- [ ] **Step 3: Add `on_delta` to `handle_user_turn`**

Edit `packages/core_agent/brain.py`. Around line 201 change:

```python
    async def handle_user_turn(self, state: CallState, user_text: str) -> BrainTurnResult:
```

to:

```python
    async def handle_user_turn(
        self, state: CallState, user_text: str,
        on_delta=None,
    ) -> BrainTurnResult:
        """on_delta: optional Callable[[str], Awaitable[None]] fired
        for each streamed token from the FINAL (no-tool-calls) LLM reply.
        The caller (twilio_actor) uses this to pipe tokens into TTS as
        sentence boundaries land. When on_delta is None or the resolved
        provider lacks stream_complete, we use the batch path."""
```

- [ ] **Step 4: Wire streaming at the final-reply site**

Same file `packages/core_agent/brain.py`. The final `llm.complete(...)` call inside the tool loop is at line ~308-313. That call may return `tool_calls` (loops again) OR `text` (terminates). Streaming is only safe on the TERMINAL step.

Strategy: keep the initial batch `complete()` call to detect whether tools will be called. If `not response.tool_calls` AND `on_delta` is provided AND `hasattr(self.llm, "stream_complete")`, RE-fire the same request via `stream_complete` (no tools param, this pass) to get token-by-token deltas. Yes this is a second LLM call — but it happens only on the terminal step of the loop, and the second call hits OpenAI-style prompt caches so it's cheap. This keeps the tool-call detection logic untouched and additive.

Alternative considered + rejected: making the FIRST call streaming. Rejected because tool_calls arrive as structured deltas that Mistral's `stream_complete` doesn't parse (documented in `mistral_llm.py:108`: "No tools/response_schema support"). Streaming with tools means writing an SSE tool-call parser, out of scope.

Locate the `if not response.tool_calls:` block at line ~383 and insert BEFORE the `reply_text = sanitize_for_speech(response.text)` line:

```python
            if not response.tool_calls:
                # ── Task #283: streaming path ──────────────────────────
                # If caller provided on_delta AND provider streams,
                # re-fire as a streaming call and pump deltas out.
                # We already know no tools will be emitted (this branch),
                # so a second call is safe. Prompt cache makes it cheap.
                streaming_full_text: str | None = None
                if on_delta is not None and hasattr(self.llm, "stream_complete"):
                    try:
                        import time as _t
                        _t0 = _t.perf_counter()
                        import logging as _sl
                        _slog = _sl.getLogger(__name__)
                        _slog.info(
                            "LLM_STREAM_START session=%s provider=%s model=%s",
                            state.session_id,
                            getattr(self.llm, "name", "?"),
                            getattr(self.llm, "model", "?"),
                        )
                        chunks: list[str] = []
                        first_token_ms: float | None = None
                        async for delta, is_final in self.llm.stream_complete(
                            messages, temperature=0.3, max_tokens=300,
                        ):
                            if delta:
                                if first_token_ms is None:
                                    first_token_ms = (_t.perf_counter() - _t0) * 1000
                                    _slog.info(
                                        "LLM_FIRST_TOKEN session=%s first_token_ms=%.0f",
                                        state.session_id, first_token_ms,
                                    )
                                chunks.append(delta)
                                try:
                                    await on_delta(delta)
                                except Exception as _cbe:
                                    _slog.warning("on_delta raised: %s", _cbe)
                            if is_final:
                                break
                        streaming_full_text = "".join(chunks)
                        _slog.info(
                            "LLM_STREAM_DONE session=%s chars=%d total_ms=%.0f",
                            state.session_id, len(streaming_full_text),
                            (_t.perf_counter() - _t0) * 1000,
                        )
                    except NotImplementedError:
                        # Router picked a non-streaming provider — fall
                        # through to batch response.text
                        streaming_full_text = None
                    except Exception as _se:
                        import logging as _sl
                        _sl.getLogger(__name__).warning(
                            "streaming path failed, falling back to batch: %s",
                            _se,
                        )
                        streaming_full_text = None

                raw_text = streaming_full_text if streaming_full_text else response.text
                # Sanitize before speaking: strip (parentheses), <angle brackets>,
                # tool-name leakage, and expand common abbreviations. Belt-and-
                # suspenders for prompt rules the LLM sometimes ignores.
                reply_text = sanitize_for_speech(raw_text)
```

Then DELETE the now-duplicated `reply_text = sanitize_for_speech(response.text)` line that follows it. The rest of the block (fake-booking guard, `state.add_turn`, `return BrainTurnResult(...)`) stays exactly as-is because it operates on `reply_text`.

- [ ] **Step 5: Add `on_delta` to `session_manager.run_user_turn`**

Edit `apps/api/app/core/session_manager.py:317`:

```python
async def run_user_turn(
    state: CallState, brain: ReceptionistBrain, user_text: str,
    on_delta=None,
) -> dict:
    result = await brain.handle_user_turn(state, user_text, on_delta=on_delta)
```

- [ ] **Step 6: Run all streaming tests**

Run: `cd "/Users/az/Desktop/Receptionist Agent" && python -m pytest apps/api/tests/test_streaming_llm_pipeline.py -v`
Expected: 11 passed

- [ ] **Step 7: Run the pre-existing brain suite to ensure no regression**

Run: `cd "/Users/az/Desktop/Receptionist Agent" && python -m pytest packages/core_agent -v 2>&1 | tail -30`
Expected: same pass/fail counts as before the change (this task adds an optional kwarg only)

- [ ] **Step 8: Commit**

```bash
git add packages/core_agent/brain.py apps/api/app/core/session_manager.py apps/api/tests/test_streaming_llm_pipeline.py
git commit -m "$(cat <<'EOF'
feat(task-283): thread on_delta callback through brain + session_manager

Optional kwarg. When caller supplies on_delta AND provider has
stream_complete AND the tool loop resolved with no tool_calls, the
brain re-fires the final reply as a streaming call and pumps deltas
to the callback. Batch fallback on NotImplementedError / provider
error. Existing callers unchanged.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Actor streaming path with per-sentence TTS dispatch (audible milestone)

**Files:**
- Modify: `apps/api/app/routes/twilio_actor.py:895-940` (add `_run_brain_streaming` + `_stream_sentence_to_tts` helper, wire in `_run_brain`)
- Test: manual browser verification (steps below)

**Interfaces:**
- Consumes: `SentenceBuffer` (Task 1), `handle_user_turn(..., on_delta=...)` (Task 3), existing `_stream_tts_incremental` (in same file line ~1260)
- Produces: no exports; strictly internal to the actor

- [ ] **Step 1: Add SentenceBuffer import at top of twilio_actor.py**

Edit `apps/api/app/routes/twilio_actor.py`. Find the existing `from packages.` imports block near the top. Add:

```python
from packages.core_agent.streaming import SentenceBuffer
```

- [ ] **Step 2: Add the streaming-eligible gate + dispatcher helper**

Add these two methods inside the actor class (near `_run_brain`, before line 895). The `_stream_sentence_to_tts` helper is a queue-fed worker so sentences dispatched in-order without blocking `on_delta`:

```python
    def _streaming_llm_eligible(self, brain) -> bool:
        """Task #283: gate the streaming LLM→TTS path.

        Off unless the flag is on AND the resolved provider has
        stream_complete AND we're on the phone leg AND VPL is off.
        Tool-call turns are NOT gated here — brain.handle_user_turn
        auto-falls-through when tools are called (streaming only fires
        on the terminal no-tools branch)."""
        if not settings.streaming_llm_to_tts:
            return False
        if settings.two_planner_enabled:
            return False
        if self.stream_sid.startswith("browser_"):
            return False
        if not hasattr(brain.llm, "stream_complete"):
            return False
        return True

    async def _pump_sentence_queue(
        self, queue: asyncio.Queue, gen: int,
    ) -> None:
        """Consumer: takes sentences off the queue and pipes each into
        _stream_tts_incremental sequentially. Stops when it sees None.
        Runs as a background task spawned from _run_brain_streaming."""
        from app.routes.twilio import _get_telephony_tts
        tts = _get_telephony_tts()
        span = self._current_turn_span
        first = True
        while True:
            sentence = await queue.get()
            if sentence is None:
                break
            if gen != self.speech_generation:
                log.info(
                    "TTS_SENTENCE_DROPPED_STALE call=%s stale_gen=%d cur_gen=%d",
                    self.call_id, gen, self.speech_generation,
                )
                continue
            try:
                from packages.voice.speech_sanitizer import sanitize_for_speech
                clean = sanitize_for_speech(sentence)
                if not clean.strip():
                    continue
                log.info(
                    "TTS_SENTENCE_QUEUED call=%s gen=%d first=%s text=%r",
                    self.call_id, gen, first, clean[:80],
                )
                await self._stream_tts_incremental(tts, clean, gen, span if first else None)
                first = False
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("TTS_SENTENCE_FAILED: %s", e)
```

- [ ] **Step 3: Rewrite `_run_brain` to branch into the streaming path**

Edit `apps/api/app/routes/twilio_actor.py` around line 895. Replace the whole `_run_brain` method with:

```python
    async def _run_brain(self, mulaw: bytes, turn_gen: int) -> None:
        from app.routes.twilio import _mulaw_frames_to_wav
        span = self._current_turn_span
        try:
            wav = _mulaw_frames_to_wav(mulaw)
            stt = get_stt()
            transcript = await stt.transcribe(
                wav, sample_rate=TWILIO_SAMPLE_RATE, mime="audio/wav",
            )
            if span is not None:
                span.mark("stt_first_partial")
                span.mark("stt_final")
            if not transcript.strip():
                return

            log.info("actor %s turn=%d heard: %s",
                     self.session_id, turn_gen, transcript)
            handle = session_manager.get_session(
                self.session_id, tenant_id=self.tenant_id,
            )
            if handle is None:
                state, brain = session_manager.start_session_with_id(
                    self.session_id, tenant_id=self.tenant_id,
                )
            else:
                state, brain = handle

            if self._streaming_llm_eligible(brain):
                await self._run_brain_streaming(state, brain, transcript, turn_gen, span)
            else:
                payload = await session_manager.run_user_turn(state, brain, transcript)
                if span is not None:
                    span.mark("llm_first_token")
                reply = (payload.get("reply") or "").strip()
                self._current_speech_act = _infer_speech_act_from_payload(payload)
                if reply:
                    await self._speak(reply)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("actor _run_brain failed: %s", e)
```

- [ ] **Step 4: Add the `_run_brain_streaming` method**

Immediately after the updated `_run_brain`, add:

```python
    async def _run_brain_streaming(
        self, state, brain, transcript: str, turn_gen: int, span,
    ) -> None:
        """Task #283: streaming LLM→TTS.

        Callback pushes tokens into a SentenceBuffer. Each complete
        sentence goes onto a queue that a background pumper feeds into
        _stream_tts_incremental in order. When brain finishes:
          - If fake-booking guard trips: bump_turn to cancel in-flight
            audio, then speak the safe fallback.
          - Otherwise: flush any residual tokens as a final sentence."""
        buf = SentenceBuffer(min_first_chars=20)
        queue: asyncio.Queue = asyncio.Queue()
        pumper_task = asyncio.create_task(
            self._pump_sentence_queue(queue, turn_gen),
            name=f"tts-pump-{self.call_id}-g{turn_gen}",
        )
        first_delta = True

        async def on_delta(delta: str):
            nonlocal first_delta
            if first_delta and span is not None:
                span.mark("llm_first_token")
                first_delta = False
            for sentence in buf.push(delta):
                await queue.put(sentence)

        try:
            payload = await session_manager.run_user_turn(
                state, brain, transcript, on_delta=on_delta,
            )
            self._current_speech_act = _infer_speech_act_from_payload(payload)

            # Flush residual (text after the last sentence-ender)
            residual = buf.flush()
            if residual:
                await queue.put(residual)

            # Signal end-of-stream to the pumper
            await queue.put(None)
            await pumper_task

            # Reconcile the ledger with the full text spoken (barge-in
            # heard-vs-generated needs this). The pumper wrote chunks
            # into the ledger per sentence; overwrite the running
            # generation's full_text so it matches what we planned.
            try:
                if self.actor is not None:
                    entry = self.actor.ledger._generations.get(turn_gen)
                    if entry is not None:
                        entry.full_text = buf.full_text
            except Exception:
                pass

            # If the brain replaced the reply due to fake-booking guard,
            # the payload["reply"] won't match buf.full_text. That means
            # the guard tripped AFTER we already spoke some (or all) of
            # the streamed content. Interrupt what we sent + speak the
            # replacement.
            planned = (payload.get("reply") or "").strip()
            if planned and planned != buf.full_text.strip():
                log.warning(
                    "STREAM_REPLY_REPLACED call=%s gen=%d spoken=%r planned=%r",
                    self.call_id, turn_gen,
                    buf.full_text[:100], planned[:100],
                )
                await self._send_twilio_clear()
                await self._speak(planned)
        except asyncio.CancelledError:
            pumper_task.cancel()
            raise
        except Exception as e:
            log.exception("_run_brain_streaming failed: %s", e)
            pumper_task.cancel()
```

- [ ] **Step 5: Verify imports resolve + module loads**

Run: `cd "/Users/az/Desktop/Receptionist Agent/apps/api" && python -c "from app.routes.twilio_actor import CallActor; print('imports ok'); import inspect; print('has _run_brain_streaming:', hasattr(CallActor, '_run_brain_streaming'))"`
Expected: `imports ok` + `has _run_brain_streaming: True`

- [ ] **Step 6: Run the existing actor test suite for regressions**

Run: `cd "/Users/az/Desktop/Receptionist Agent" && python -m pytest apps/api/tests/test_twilio_actor.py apps/api/tests/test_call_actor.py -v 2>&1 | tail -20`
Expected: same pass/fail as before (flag defaults off → streaming path unreachable)

- [ ] **Step 7: Manual browser verification with flag OFF (regression check)**

Restart the server:
```bash
bash "/Users/az/Desktop/Receptionist Agent/apps/api/scripts/run_server.sh"
```

Open the browser widget at `http://localhost:8000/call`. Make a call. Say "what are your hours". Expect: reply plays normally (batch path), no crashes, latency same as before.

If regression → back out and diagnose. If clean → proceed to Step 8.

- [ ] **Step 8: Manual browser verification with flag ON**

Kill server, then:
```bash
export STREAMING_LLM_TO_TTS=true
export LLM_ROUTER_ORDER=mistral,groq,cerebras
bash "/Users/az/Desktop/Receptionist Agent/apps/api/scripts/run_server.sh"
```

Open widget, say "tell me about your clinic" (long non-tool reply that Mistral will stream). Watch the log for:
- `LLM_STREAM_START`
- `LLM_FIRST_TOKEN session=... first_token_ms=<value>` — should be 200-500ms
- Multiple `TTS_SENTENCE_QUEUED` lines
- `LLM_STREAM_DONE`

Audibly: you should hear the first sentence start speaking BEFORE the log line `LLM_STREAM_DONE` fires. That's the whole payoff.

Note: BROWSER path won't stream (gate blocks browser_ stream_sid). Test in browser only confirms batch fallback stays clean. Real payoff verification is on the phone leg.

- [ ] **Step 9: Commit**

```bash
git add apps/api/app/routes/twilio_actor.py
git commit -m "$(cat <<'EOF'
feat(task-283): streaming LLM→TTS actor path

New _run_brain_streaming branch fires when eligible: pushes deltas
into SentenceBuffer, each complete sentence hits a queue that a
background pumper feeds into _stream_tts_incremental in order.
Guard cases: fake-booking-guard replacement, stale generation drops,
CancelledError propagation. Ledger full_text reconciled at stream end.

Feature flag STREAMING_LLM_TO_TTS defaults off. Gated to phone leg
+ no-VPL + Mistral-family providers.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Phone leg verification + telemetry doc

**Files:**
- Modify: `docs/rnd-2026-08/38-sprint9-plan.md` (append a "task 283 status" section — this doc is already gitignored per user's rules; skip if it doesn't exist)
- Test: real phone call verification (Twilio inbound)

**Interfaces:**
- Consumes: everything from Tasks 1-4
- Produces: telemetry sample + first-token-ms number for the memory system

- [ ] **Step 1: Wire the flag in .env**

Edit `/Users/az/Desktop/Receptionist Agent/.env`. Add (or update) these lines:

```
STREAMING_LLM_TO_TTS=true
LLM_ROUTER_ORDER=mistral,groq,cerebras,fireworks,openai,gemini
```

(Router order: put Mistral first so the streaming path actually fires. If mistral rate-limits or errors, the router falls to a non-streaming provider and the streaming path short-circuits to batch inside brain.handle_user_turn's NotImplementedError branch.)

- [ ] **Step 2: Restart server + tail logs**

```bash
bash "/Users/az/Desktop/Receptionist Agent/apps/api/scripts/run_server.sh"
```

In a second terminal:
```bash
tail -F "/Users/az/Desktop/Receptionist Agent/apps/api/data/logs/uvicorn.log" | grep -E "LLM_STREAM|LLM_FIRST_TOKEN|TTS_SENTENCE|TTS_STREAM_START|TTS_FIRST_BYTE"
```

- [ ] **Step 3: Place a phone call to the Twilio number**

Speak a prompt that will produce a multi-sentence reply without tool calls:
- "Tell me about your clinic"
- "What kind of dental work do you do"
- "Who are your dentists"

- [ ] **Step 4: Read the log and record numbers**

Copy the log lines from that turn into `/tmp/task283-verification.txt`. Extract:
- `first_token_ms` (should be 250-500ms from PK)
- Time from `LLM_STREAM_START` to first `TTS_FIRST_BYTE` (this is the perceived-latency win)
- Number of `TTS_SENTENCE_QUEUED` events

Compare against a batch-path turn (set `STREAMING_LLM_TO_TTS=false`, restart, place another call) to get the delta.

- [ ] **Step 5: Record the finding as an auto-memory**

Write `/Users/az/.claude/projects/-Users-az-Desktop-Receptionist-Agent/memory/streaming-llm-tts-bench.md` with the measured deltas:

```markdown
---
name: streaming-llm-tts-bench
description: Streaming LLM→TTS measured savings on the phone leg
metadata:
  type: project
---

Task #283 shipped 2026-08-12 (`STREAMING_LLM_TO_TTS=true` in .env).

Measured on inbound Twilio call from PK, Mistral as primary:
- Batch path: first audio byte at ~<X>ms after brain fires
- Stream path: first audio byte at ~<Y>ms after brain fires
- Saved: ~<X-Y>ms perceived latency per turn

**How to apply:** Keep `STREAMING_LLM_TO_TTS=true` for demo builds. Turn
OFF when investigating fake-booking regressions — the streaming path
has to interrupt in-flight audio when the guard trips (see
`_run_brain_streaming` STREAM_REPLY_REPLACED log line), which is
harder to reason about than the batch guard.

Only fires when: `mistral` is the resolved provider (only one with
`stream_complete` today), not two-planner, not browser leg, no tools.

Related: [[elevenlabs-ws-bench-finding]] — WS TTS becomes the next
lever ONCE we're streaming tokens (Task 6, Phase 2).
```

Then append to `MEMORY.md`:

```markdown
- [Streaming LLM→TTS bench](streaming-llm-tts-bench.md) — Measured savings + when to disable
```

- [ ] **Step 6: Commit**

```bash
git add .env
git commit -m "$(cat <<'EOF'
chore(task-283): enable STREAMING_LLM_TO_TTS + pin mistral primary

Streaming path only fires when the router picks a provider with
stream_complete (only Mistral today). Pin it first in LLM_ROUTER_ORDER
so we actually get the streaming path. Falls back to batch invisibly
on Mistral rate-limit / error.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

(The memory files live outside the repo and don't get committed.)

---

## Task 6: Phase 2 — WS incremental push (unlock the elevenlabs_use_ws win)

**Files:**
- Modify: `apps/api/app/providers/tts/elevenlabs_tts.py:122-219` (extend `ws_stream_synthesize` to accept an async iterator of text chunks in addition to a full string)
- Modify: `apps/api/app/routes/twilio_actor.py` (`_pump_sentence_queue` → optional WS-per-turn mode)
- Modify: `apps/api/app/core/config.py` (add `elevenlabs_ws_incremental: bool = False`)

**Interfaces:**
- Consumes: existing `ws_stream_synthesize` shape from `elevenlabs_tts.py`; `SentenceBuffer` (Task 1)
- Produces: new overload
  ```python
  async def ws_stream_synthesize(
      self,
      text_or_iter,  # str OR AsyncIterator[str]
      voice: Optional[str] = None,
  ) -> AsyncIterator[tuple[bytes, str]]: ...
  ```

- [ ] **Step 1: Add config flag for the Phase 2 mode**

Edit `apps/api/app/core/config.py`. After `elevenlabs_use_ws`:

```python
    # 2026-08-12 (task #283 Phase 2): use ONE WS connection per turn,
    # pushing sentences into it incrementally as the LLM produces them.
    # Requires streaming_llm_to_tts=True. When ON, elevenlabs_use_ws is
    # implicitly True (the streaming path OWNS the WS).
    elevenlabs_ws_incremental: bool = False
```

- [ ] **Step 2: Extend `ws_stream_synthesize` to accept an async iterator**

Edit `apps/api/app/providers/tts/elevenlabs_tts.py:122`. Change signature and body to accept either a `str` or an `AsyncIterator[str]`. The str path stays identical (send init, empty-text close, drain). The iterator path:
1. Sends init with the first chunk (or `" "` if the iterator hasn't yielded yet — send init early to warm the model)
2. For each subsequent chunk, sends `{"text": chunk, "try_trigger_generation": true}` (ElevenLabs API param that flushes the current buffer to synth)
3. When iterator is exhausted, sends `{"text": ""}` to close
4. Drains audio messages concurrently the whole time (needs `asyncio.gather` on send loop + recv loop)

Replace the whole method with:

```python
    async def ws_stream_synthesize(
        self,
        text_or_iter,
        voice: Optional[str] = None,
    ):
        """WS streaming. Accepts either a full string OR an async
        iterator of text chunks (sentences from the LLM stream).

        String mode (Phase 1): unchanged behavior; adapter sends full
        text in one message, waits for isFinal.

        Iterator mode (Phase 2, task #283): opens ONE WS per turn,
        sends the first sentence with voice_settings, then sends each
        subsequent sentence with try_trigger_generation=true so the
        model flushes without waiting for input to end. Audio drains
        concurrently. Closes with empty text when the iterator ends.
        """
        if not self.api_key:
            raise RuntimeError("ELEVENLABS_API_KEY not set")
        try:
            import websockets
        except ImportError as e:
            raise RuntimeError(
                "ElevenLabs WS streaming needs `pip install websockets`.",
            ) from e
        import base64 as _b64
        import logging as _l
        import time as _t
        _log = _l.getLogger(__name__)

        voice_id = voice or self.default_voice
        url = (
            f"wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream-input"
            f"?model_id={self.model}"
            f"&output_format={self._eleven_fmt}"
            f"&auto_mode=true"
            f"&inactivity_timeout=30"
        )
        headers = {"xi-api-key": self.api_key}

        t_open_start = _t.perf_counter()
        try:
            _ws_ctx = websockets.connect(url, additional_headers=headers)
            ws = await _ws_ctx.__aenter__()
        except TypeError:
            _ws_ctx = websockets.connect(url, extra_headers=headers)
            ws = await _ws_ctx.__aenter__()
        t_open_ms = (_t.perf_counter() - t_open_start) * 1000

        # Detect mode. hasattr(x, '__aiter__') catches async iterators
        # AND async generators.
        is_iter = hasattr(text_or_iter, "__aiter__")

        # Bounded queue of audio chunks emitted by the reader task,
        # yielded by this generator.
        audio_out: asyncio.Queue = asyncio.Queue()
        first_chunk_ms: Optional[float] = None
        t_send = _t.perf_counter()

        async def sender():
            try:
                if is_iter:
                    first = True
                    async for chunk in text_or_iter:
                        if not chunk:
                            continue
                        if first:
                            init = json.dumps({
                                "text": chunk + " ",
                                "voice_settings": {
                                    "stability": 0.5,
                                    "similarity_boost": 0.75,
                                },
                                "generation_config": {
                                    "chunk_length_schedule": [50, 90, 160, 250],
                                },
                            })
                            await ws.send(init)
                            first = False
                        else:
                            await ws.send(json.dumps({
                                "text": chunk + " ",
                                "try_trigger_generation": True,
                            }))
                    # End of iterator — close stream
                    await ws.send(json.dumps({"text": ""}))
                else:
                    text: str = text_or_iter
                    init = json.dumps({
                        "text": text + " ",
                        "voice_settings": {
                            "stability": 0.5,
                            "similarity_boost": 0.75,
                        },
                        "generation_config": {
                            "chunk_length_schedule": [50, 90, 160, 250],
                        },
                    })
                    await ws.send(init)
                    await ws.send(json.dumps({"text": ""}))
            except Exception as se:
                _log.warning("ELEVEN_WS sender failed: %s", se)

        async def reader():
            nonlocal first_chunk_ms
            chunks = 0
            total_bytes = 0
            try:
                while True:
                    try:
                        raw = await ws.recv()
                    except websockets.ConnectionClosed:
                        break
                    try:
                        msg = json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode())
                    except Exception:
                        continue
                    if msg.get("audio"):
                        audio_bytes = _b64.b64decode(msg["audio"])
                        if first_chunk_ms is None:
                            first_chunk_ms = (_t.perf_counter() - t_send) * 1000
                            _log.info(
                                "ELEVEN_WS first_chunk_ms=%.0f connect_ms=%.0f voice=%s mode=%s",
                                first_chunk_ms, t_open_ms, voice_id[:8],
                                "iter" if is_iter else "str",
                            )
                        chunks += 1
                        total_bytes += len(audio_bytes)
                        await audio_out.put((audio_bytes, self.mime))
                    if msg.get("isFinal"):
                        break
            finally:
                await audio_out.put(None)  # sentinel
                _log.info(
                    "ELEVEN_WS done chunks=%d bytes=%d first_ms=%s mode=%s",
                    chunks, total_bytes,
                    f"{first_chunk_ms:.0f}" if first_chunk_ms else "?",
                    "iter" if is_iter else "str",
                )

        sender_task = asyncio.create_task(sender())
        reader_task = asyncio.create_task(reader())
        try:
            while True:
                item = await audio_out.get()
                if item is None:
                    break
                yield item
        finally:
            sender_task.cancel()
            reader_task.cancel()
            try:
                await _ws_ctx.__aexit__(None, None, None)
            except Exception:
                pass
```

Note: `asyncio` needs to be imported at the top of the file. Check `elevenlabs_tts.py`'s existing imports and add `import asyncio` if absent.

- [ ] **Step 3: Actor wiring — sentence-queue-as-async-iterator mode**

Edit `apps/api/app/routes/twilio_actor.py`. Modify `_pump_sentence_queue`. When `settings.elevenlabs_ws_incremental` is on AND the TTS inner provider has `ws_stream_synthesize`, open ONE WS session for the whole turn instead of a fresh HTTP stream per sentence.

Add a new method `_pump_via_ws_incremental` next to `_pump_sentence_queue`:

```python
    async def _pump_via_ws_incremental(
        self, queue: asyncio.Queue, gen: int,
    ) -> None:
        """Phase 2 (task #283): one WS session per turn. Yield-from the
        queue as an async iterator of sentence strings; ws_stream_synthesize
        pushes them incrementally with try_trigger_generation=true."""
        from app.routes.twilio import _get_telephony_tts
        from packages.voice.speech_sanitizer import sanitize_for_speech
        tts = _get_telephony_tts()
        inner = getattr(tts, "_inner", tts)
        span = self._current_turn_span
        import time as _t
        t_open = _t.perf_counter()

        async def sentence_iter():
            while True:
                s = await queue.get()
                if s is None:
                    return
                if gen != self.speech_generation:
                    log.info(
                        "TTS_SENTENCE_DROPPED_STALE call=%s stale_gen=%d cur_gen=%d",
                        self.call_id, gen, self.speech_generation,
                    )
                    continue
                clean = sanitize_for_speech(s)
                if clean.strip():
                    log.info(
                        "TTS_SENTENCE_QUEUED call=%s gen=%d text=%r mode=ws-inc",
                        self.call_id, gen, clean[:80],
                    )
                    yield clean

        first_byte_logged = False
        try:
            async for chunk, mime in inner.ws_stream_synthesize(sentence_iter()):
                if not chunk:
                    continue
                if not first_byte_logged:
                    first_byte_logged = True
                    if span is not None:
                        span.mark("tts_first_byte")
                        self._close_turn_span()
                    log.info(
                        "TTS_FIRST_BYTE call=%s gen=%d transport=ws-inc first_byte_ms=%.0f",
                        self.call_id, gen, (_t.perf_counter() - t_open) * 1000,
                    )
                await self._send_audio_frames(chunk, mime)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("TTS_WS_INCREMENTAL_FAILED call=%s: %s", self.call_id, e)
```

Update `_run_brain_streaming` (from Task 4) to spawn `_pump_via_ws_incremental` when the incremental flag is on, else `_pump_sentence_queue`:

```python
        # Pick pumper based on WS-incremental flag
        if (
            settings.elevenlabs_ws_incremental
            and hasattr(getattr(_get_telephony_tts(), "_inner", None), "ws_stream_synthesize")
        ):
            pumper_task = asyncio.create_task(
                self._pump_via_ws_incremental(queue, turn_gen),
                name=f"tts-ws-inc-{self.call_id}-g{turn_gen}",
            )
        else:
            pumper_task = asyncio.create_task(
                self._pump_sentence_queue(queue, turn_gen),
                name=f"tts-pump-{self.call_id}-g{turn_gen}",
            )
```

(The `_get_telephony_tts` import is already present in `_pump_sentence_queue`; hoist it to the top of `_run_brain_streaming`.)

- [ ] **Step 4: Verify imports resolve**

Run: `cd "/Users/az/Desktop/Receptionist Agent/apps/api" && python -c "from app.routes.twilio_actor import CallActor; from app.providers.tts.elevenlabs_tts import ElevenLabsTTS; import inspect; sig = inspect.signature(ElevenLabsTTS.ws_stream_synthesize); print('ws sig:', sig)"`
Expected: `ws sig: (self, text_or_iter, voice: Optional[str] = None)`

- [ ] **Step 5: Standalone WS iterator bench**

Extend `/tmp/bench_eleven_ws.py` with an incremental mode. Add at the bottom of `main()`:

```python
    print("\n=== INCREMENTAL WS (sentence iterator) ===")

    async def sent_iter():
        for s in ["Hi, thanks for calling Smile Dental.",
                  "This is Alex, how can I help you today?"]:
            yield s

    t0 = time.perf_counter()
    first = None
    chunks = 0
    async for chunk, mime in tts.ws_stream_synthesize(sent_iter()):
        if first is None:
            first = (time.perf_counter() - t0) * 1000
        chunks += 1
    total = (time.perf_counter() - t0) * 1000
    print(f"  ws-inc: first={first:.0f}ms total={total:.0f}ms chunks={chunks}")
```

Run: `python3 /tmp/bench_eleven_ws.py`
Expected: WS-inc first_ms comparable to the HTTP first-byte number (~300-500ms) instead of the ~1600ms one-shot WS number. If it's still 1500+, `try_trigger_generation` didn't flush — check the WS log lines for `first_chunk_ms`.

- [ ] **Step 6: Phone leg verification**

Set both flags:
```bash
export STREAMING_LLM_TO_TTS=true
export ELEVENLABS_WS_INCREMENTAL=true
bash "/Users/az/Desktop/Receptionist Agent/apps/api/scripts/run_server.sh"
```

Place a phone call. Say "tell me about the dentists". Tail the log for:
- `TTS_FIRST_BYTE call=... transport=ws-inc first_byte_ms=<value>`

Expected: first_byte_ms in the 250-500ms range. Compare against Task 5's HTTP-per-sentence number.

- [ ] **Step 7: Commit**

```bash
git add apps/api/app/providers/tts/elevenlabs_tts.py apps/api/app/routes/twilio_actor.py apps/api/app/core/config.py
git commit -m "$(cat <<'EOF'
feat(task-283 phase 2): WS-incremental TTS — one connection per turn

ws_stream_synthesize now accepts either a full string or an async
iterator. Iterator mode opens ONE WS, pushes each sentence with
try_trigger_generation=true, drains audio concurrently. Actor spawns
_pump_via_ws_incremental when both flags on.

Feature-flagged behind ELEVENLABS_WS_INCREMENTAL (default off).
Requires STREAMING_LLM_TO_TTS=true.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Adversarial replay + fake-booking safety verification

**Files:**
- Test: `apps/api/tests/test_streaming_llm_pipeline.py` (extend with fake-booking simulation)
- Runtime verification: adversarial harness (already exists per task #92)

**Interfaces:**
- Consumes: full stack from Tasks 1-6
- Produces: a proof that streaming path doesn't create phantom bookings

- [ ] **Step 1: Write the failing test — streaming replaces reply on fake-booking**

Append to `apps/api/tests/test_streaming_llm_pipeline.py`:

```python
def test_streaming_reply_replaced_when_booking_guard_trips(monkeypatch):
    """When the streamed reply is a fake-booking confirmation and no
    booking tool ran, the brain must replace it with the safe fallback.
    The actor's _run_brain_streaming then interrupts and re-speaks.

    This test only asserts the BRAIN half — the actor half is verified
    on the phone leg (Task 7 Step 3)."""
    from packages.core_agent.brain import ReceptionistBrain
    from packages.core_agent.state import CallState
    from app.providers.base import LLMResponse

    class FakeBookingLLM:
        name = "fake-booker"
        model = "fake-1"
        async def complete(self, messages, tools=None, temperature=0.3,
                           max_tokens=1024, response_schema=None, site=None):
            return LLMResponse(
                text="You're all set for your new patient exam on May twelfth.",
                tool_calls=[],
            )
        async def stream_complete(self, messages, temperature=0.3, max_tokens=1024):
            for tok in ["You're all set for ",
                        "your new patient exam ",
                        "on May twelfth."]:
                yield tok, False
            yield "", True

    brain = ReceptionistBrain.__new__(ReceptionistBrain)
    brain.llm = FakeBookingLLM()
    brain.system_prompt = "sys"
    brain.tools = []
    brain.rag = None
    brain._refresh_extraction_bg = lambda s: None
    brain.MAX_TOOL_ITERATIONS = 4

    state = CallState(session_id="s2", business_id="b1", tenant_id="t1")
    received = []
    async def cb(delta): received.append(delta)

    result = asyncio.get_event_loop().run_until_complete(
        brain.handle_user_turn(state, "book me", on_delta=cb)
    )
    # Guard should have rewritten the reply
    assert "hold on" in result.reply.lower() or "need" in result.reply.lower()
    assert "may twelfth" not in result.reply.lower()
    # But the caller received the DANGEROUS streamed deltas —
    # actor is responsible for interrupting. This test proves the
    # brain payload will diverge from what was streamed (the trigger
    # actor uses in STREAM_REPLY_REPLACED).
    assert "".join(received).lower() != result.reply.lower()
```

- [ ] **Step 2: Run the tests**

Run: `cd "/Users/az/Desktop/Receptionist Agent" && python -m pytest apps/api/tests/test_streaming_llm_pipeline.py -v`
Expected: 12 passed

- [ ] **Step 3: Manual verification — phone call that could trigger fake-booking**

With `STREAMING_LLM_TO_TTS=true`, place a phone call. Say (verbatim, in a way that pushes the LLM toward premature confirmation):
- "Hi, book me a cleaning for tomorrow at 3pm, name's John."

The LLM may or may not attempt to confirm without calling `book_appointment`. If it does, listen for:
- Initial streamed sentence starts playing ("Great, you're booked...")
- Then abrupt cut + "Hold on, I don't have everything I need yet..."

Check the log for `STREAM_REPLY_REPLACED call=... spoken=... planned=...`. That confirms the guard fired and the actor interrupted correctly.

If audio does NOT interrupt, the actor's `_send_twilio_clear()` is not being called or Twilio isn't honoring the clear frame in time. Diagnose before proceeding.

- [ ] **Step 4: Run the adversarial harness against streaming**

Assuming the harness path is `apps/api/tests/adversarial/run_harness.py` (per task #92):

```bash
cd "/Users/az/Desktop/Receptionist Agent" && \
  STREAMING_LLM_TO_TTS=true LLM_ROUTER_ORDER=mistral,groq \
  python apps/api/tests/adversarial/run_harness.py --scenario book-attempts --limit 10
```

(If that path is wrong, find the harness with `find "/Users/az/Desktop/Receptionist Agent" -name 'run_harness*' -o -name 'adversarial*.py' | head -5` and adapt.)

Expected: pass rate equal-or-better than the batch-path baseline. If regressions on booking scenarios, the guard-plus-interrupt loop isn't cutting audio fast enough. Fix or turn the flag off.

- [ ] **Step 5: Commit**

```bash
git add apps/api/tests/test_streaming_llm_pipeline.py
git commit -m "$(cat <<'EOF'
test(task-283): fake-booking guard fires on streaming path

Guard rewrites the reply; actor detects divergence (spoken != planned)
and triggers Twilio clear + safe fallback. Adversarial harness rerun
confirms no phantom-booking regression under streaming path.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 6: Mark task #283 completed in TaskUpdate + update memory**

Run: `TaskUpdate` for task 283 → status `completed`.

Update `/Users/az/.claude/projects/-Users-az-Desktop-Receptionist-Agent/memory/streaming-llm-tts-bench.md` (created in Task 5) with the final numbers from the phone call in Task 6 Step 6 (WS-incremental first_byte_ms).

---

## Post-plan checklist

- [ ] All 7 tasks committed with individual commits (no squash)
- [ ] `streaming_llm_to_tts=True` and `elevenlabs_ws_incremental=True` both on for the demo build
- [ ] Router order pins Mistral first
- [ ] `/tmp/bench_eleven_ws.py` extended with the incremental mode
- [ ] Memory file `streaming-llm-tts-bench.md` populated with real ms numbers
- [ ] Old batch path still works — verified by flipping flag off and calling once

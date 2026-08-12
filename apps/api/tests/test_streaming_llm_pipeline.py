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


# ── Task 2: LLMProvider.stream_complete ──────────────────────────────

import asyncio
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
    agen = llm.stream_complete([{"role": "user", "content": "hi"}])
    with pytest.raises(NotImplementedError):
        asyncio.get_event_loop().run_until_complete(agen.__anext__())


def test_mistral_still_has_stream_complete():
    # Regression guard — task 283 depends on this method's existence
    assert hasattr(MistralLLM, "stream_complete")


# ── Task 3: on_delta plumbing through brain + session_manager ─────────

def test_handle_user_turn_accepts_on_delta_kwarg():
    """Signature check — guards accidental removal of the on_delta kwarg."""
    from packages.core_agent.brain import ReceptionistBrain
    import inspect
    sig = inspect.signature(ReceptionistBrain.handle_user_turn)
    assert "on_delta" in sig.parameters
    assert sig.parameters["on_delta"].default is None


def test_run_user_turn_accepts_on_delta_kwarg():
    from app.core.session_manager import run_user_turn
    import inspect
    sig = inspect.signature(run_user_turn)
    assert "on_delta" in sig.parameters
    assert sig.parameters["on_delta"].default is None


def test_handle_user_turn_invokes_on_delta_when_streaming():
    """Brain calls on_delta with each streamed token when the provider
    exposes stream_complete AND the reply resolves without tool_calls."""
    from packages.core_agent.brain import ReceptionistBrain
    from packages.schemas import CallState
    from app.providers.base import LLMResponse

    class StubStreamLLM:
        name = "stub"
        model = "stub-1"
        async def complete(self, messages, tools=None, temperature=0.3,
                           max_tokens=1024, response_schema=None,
                           site=None):
            return LLMResponse(text="Hello there. All good.", tool_calls=[])
        async def stream_complete(self, messages, temperature=0.3,
                                  max_tokens=1024):
            for tok in ["Hello ", "there. ", "All ", "good."]:
                yield tok, False
            yield "", True

    brain = ReceptionistBrain.__new__(ReceptionistBrain)
    brain.llm = StubStreamLLM()
    brain.system_prompt = "sys"
    brain.tools = []
    brain.rag = None
    brain._refresh_extraction_bg = lambda s: None
    brain._kernel = None
    brain._get_kernel = lambda: None
    brain.MAX_TOOL_ITERATIONS = 4

    state = CallState(session_id="s1", business_id="b1", tenant_id="t1")

    received = []
    async def cb(delta: str):
        received.append(delta)

    result = asyncio.get_event_loop().run_until_complete(
        brain.handle_user_turn(state, "hi", on_delta=cb)
    )
    assert result.reply.strip() == "Hello there. All good."
    assert "".join(received) == "Hello there. All good."

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

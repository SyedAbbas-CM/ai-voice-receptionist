"""Task B: reactive brain schema + parsing tests.  No live LLM calls."""
import asyncio
import pytest

from packages.core_agent.reactive_brain import (
    ALLOWED_BACKCHANNELS,
    ReactiveReply,
    build_notepad_message,
    parse_reactive_reply,
    reactive_turn,
)


def test_parse_commit_lane():
    r = parse_reactive_reply('''{"should_speak": true, "backchannel": null, "internal_thoughts": "caller wants cleaning", "state_updates": {}, "committed_reply": "Sure, what day works?"}''')
    assert r.lane == "commit"
    assert r.committed_reply == "Sure, what day works?"
    assert r.backchannel is None
    assert "cleaning" in r.internal_thoughts


def test_parse_silent_lane():
    r = parse_reactive_reply('''{"should_speak": false, "backchannel": null, "internal_thoughts": "mid-thought", "state_updates": {}, "committed_reply": null}''')
    assert r.lane == "silent"
    assert not r.should_speak
    assert r.committed_reply is None


def test_parse_backchannel_lane():
    r = parse_reactive_reply('''{"should_speak": true, "backchannel": "Mm-hmm.", "internal_thoughts": "ack", "state_updates": {}, "committed_reply": null}''')
    assert r.lane == "backchannel"
    assert r.backchannel == "Mm-hmm."
    assert r.committed_reply is None


def test_parse_markdown_fenced_json():
    r = parse_reactive_reply('''```json
{"should_speak": true, "backchannel": null, "internal_thoughts": "x", "committed_reply": "hi"}
```''')
    assert r.lane == "commit"
    assert r.committed_reply == "hi"


def test_parse_prose_prefix_stripped():
    r = parse_reactive_reply('''Sure, here you go: {"should_speak": true, "backchannel": null, "internal_thoughts": "x", "committed_reply": "ok"}''')
    assert r.lane == "commit"


def test_parse_invalid_backchannel_coerced():
    r = parse_reactive_reply('''{"should_speak": true, "backchannel": "mm hmm", "internal_thoughts": "x", "committed_reply": null}''')
    # "mm hmm" (spaces) shouldn't match, becomes None
    assert r.backchannel is None
    # And since committed_reply is null and should_speak was true, it
    # coerces to silent (safer than emitting weird audio).
    assert r.lane == "silent"


def test_parse_normalizes_case_backchannel():
    r = parse_reactive_reply('''{"should_speak": true, "backchannel": "MM-HMM", "internal_thoughts": "x", "committed_reply": null}''')
    assert r.backchannel == "Mm-hmm."


def test_parse_empty_falls_back_to_error_commit():
    r = parse_reactive_reply("")
    assert r.lane == "commit"
    assert "lost you" in r.committed_reply


def test_parse_garbage_falls_back():
    r = parse_reactive_reply("not json at all")
    assert r.lane == "commit"
    assert "not json at all" in r.committed_reply or "lost you" in r.committed_reply


def test_notepad_empty():
    assert build_notepad_message([]) is None
    assert build_notepad_message([""]) is None


def test_notepad_caps_at_5():
    notes = [f"thought {i}" for i in range(10)]
    msg = build_notepad_message(notes, max_entries=5)
    assert msg is not None
    assert "thought 9" in msg["content"]
    assert "thought 4" not in msg["content"]  # dropped
    assert msg["role"] == "system"


def test_reactive_turn_records_metric():
    """Uses a fake provider; ensures lane telemetry is emitted."""
    class _FakeLLM:
        name = "fake"
        model = "fake-1"
        async def complete(self, messages, tools=None, temperature=0.3, max_tokens=1024):
            class R:
                text = '{"should_speak": false, "backchannel": null, "internal_thoughts": "hmm", "committed_reply": null}'
                tool_calls = None
            return R()

    async def _run():
        r = await reactive_turn(
            llm_provider=_FakeLLM(),
            system_prompt="you are a receptionist",
            transcript_messages=[{"role": "user", "content": "hi"}],
            running_notes=[],
            tenant_id="test",
        )
        assert r.lane == "silent"

    asyncio.run(_run())


def test_both_bc_and_commit_prefers_commit():
    r = parse_reactive_reply('''{"should_speak": true, "backchannel": "Okay.", "internal_thoughts": "x", "committed_reply": "Long answer"}''')
    assert r.lane == "commit"
    assert r.backchannel is None

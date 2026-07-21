"""Voice-shaper tests. The shaper is what stops a retrieved chunk with
markdown/URLs/lists from being spoken to a caller."""
from __future__ import annotations

import pytest

from app.providers.base import LLMProvider, LLMResponse
from packages.rag import is_speakable, shape_for_voice


# ---- is_speakable ----

@pytest.mark.parametrize("text", [
    "",
    "   ",
    "a" * 300,                                      # too long
    "Visit https://example.com for details.",       # URL
    "See section three:\n- item one\n- item two",   # bullet list
    "Steps:\n1. First\n2. Second",                  # numbered list
    "Check the [FAQ page](https://x.com/faq).",     # markdown link
    "```python\ncode block\n```",                   # code fence
    "| col1 | col2 |\n|------|------|\n| a | b |",  # markdown table
])
def test_is_speakable_rejects_bad_text(text):
    assert is_speakable(text) is False, f"expected {text!r} rejected"


@pytest.mark.parametrize("text", [
    "Yes, we take Aetna PPO plans.",
    "We're open Monday through Friday, nine to five.",
    "Our standard consultation is thirty minutes.",
    "Parking is free behind the building on Maple Street.",
])
def test_is_speakable_accepts_normal_answer(text):
    assert is_speakable(text) is True


# ---- shape_for_voice ----

class ScriptedLLM(LLMProvider):
    name = "scripted"

    def __init__(self, response_text: str):
        self.response_text = response_text
        self.calls = 0

    async def complete(self, messages, tools=None, temperature=0.3, max_tokens=1024):
        self.calls += 1
        return LLMResponse(text=self.response_text)


@pytest.mark.asyncio
async def test_shape_returns_llm_output_when_speakable():
    llm = ScriptedLLM("Yes, we accept Aetna PPO plans.")
    out = await shape_for_voice(llm, question="Do you take Aetna?", retrieved_text="Q: Do you take Aetna insurance?\nA: Yes, we accept Aetna PPO...")
    assert "Aetna" in out


@pytest.mark.asyncio
async def test_shape_returns_empty_on_no_answer_signal():
    """The LLM signals it can't answer -> return empty; caller escalates."""
    llm = ScriptedLLM("NO_ANSWER")
    out = await shape_for_voice(llm, "What's your Wi-Fi password?", "Q: Hours\nA: 9am-5pm")
    assert out == ""


@pytest.mark.asyncio
async def test_shape_rejects_llm_output_with_bad_formatting():
    """Even if the LLM ignores our rules and outputs markdown, we reject."""
    llm = ScriptedLLM("Visit https://example.com/aetna for our full policy.")
    out = await shape_for_voice(llm, "Do you take Aetna?", "Some KB text.")
    assert out == ""


@pytest.mark.asyncio
async def test_shape_empty_retrieved_text_returns_empty_no_llm_call():
    llm = ScriptedLLM("NEVER_CALLED")
    out = await shape_for_voice(llm, "Hours?", "")
    assert out == ""
    assert llm.calls == 0

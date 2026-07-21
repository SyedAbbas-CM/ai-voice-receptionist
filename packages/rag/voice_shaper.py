"""Voice-shaper: retrieved chunk -> speakable one-sentence answer.

Full LLM-based shaping lands in Task 5. This file exists so the package
`__init__.py` imports cleanly during scaffolding.

Two responsibilities:
  1. is_speakable(text) — quick filter that rejects text with URLs,
     markdown syntax, tables, or excessive length.
  2. shape_for_voice(...) — LLM pass that rewrites a retrieved chunk
     as one spoken sentence in the receptionist's voice.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from apps.api.app.providers.base import LLMProvider


_UNSPEAKABLE_PATTERNS = [
    re.compile(r"https?://\S+"),           # URLs
    re.compile(r"\|.*\|"),                  # markdown tables
    re.compile(r"^\s*[-*+]\s+", re.MULTILINE),  # bullet lists
    re.compile(r"^\s*\d+\.\s+", re.MULTILINE),  # numbered lists
    re.compile(r"```"),                     # code fences
    re.compile(r"\[.+?\]\(.+?\)"),          # markdown links
]

_MAX_SPEAKABLE_CHARS = 250    # ~40 words, ~15s spoken


def is_speakable(text: str) -> bool:
    """Quick filter — return False if the text contains unspeakable
    formatting, is empty/whitespace, or is too long to speak in one breath."""
    if not text or not text.strip():
        return False
    if len(text) > _MAX_SPEAKABLE_CHARS:
        return False
    for pattern in _UNSPEAKABLE_PATTERNS:
        if pattern.search(text):
            return False
    return True


VOICE_SHAPER_PROMPT = """You are rewriting a knowledge-base entry as a single spoken answer for a phone caller.

Rules:
- ONE sentence, maximum two.
- No URLs, no bullet points, no markdown, no lists.
- No "based on our records" or "according to the document" — just answer.
- If the entry doesn't answer the question, output exactly: NO_ANSWER
- Keep numbers spoken naturally: "nine to five", "twenty-five dollars".
- Warm tone, not formal.
"""


async def shape_for_voice(
    llm: "LLMProvider",
    question: str,
    retrieved_text: str,
    max_tokens: int = 64,
) -> str:
    """Ask a cheap LLM to rewrite the retrieved chunk as a spoken answer.

    Returns the speakable string, or empty string on failure. Callers
    check emptiness and either escalate or fall back to a canned reply.
    """
    if not retrieved_text.strip():
        return ""

    resp = await llm.complete(
        messages=[
            {"role": "system", "content": VOICE_SHAPER_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Caller asked: {question}\n\n"
                    f"Knowledge base entry: {retrieved_text}\n\n"
                    "Rewrite as one spoken sentence."
                ),
            },
        ],
        tools=None,
        temperature=0.2,
        max_tokens=max_tokens,
    )
    text = (resp.text or "").strip()

    # Explicit no-answer signal from the shaper
    if text.upper().startswith("NO_ANSWER"):
        return ""

    # Post-filter: if the shaper ignored our formatting rules, reject
    if not is_speakable(text):
        return ""

    return text

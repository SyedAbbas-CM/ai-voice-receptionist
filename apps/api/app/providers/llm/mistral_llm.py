"""Mistral La Plateforme — OpenAI-compatible.

Docs: https://docs.mistral.ai/api/
Notable: 8 working models incl. mistral-large-latest, mistral-small-latest,
ministral-3b/8b, codestral, pixtral (multimodal), open-mistral-nemo (12B).
EU-hosted — adds geographic redundancy to a US-provider-heavy router.

Added 2026-08-04 to expand router coverage beyond the original
Groq/Gemini/NVIDIA/OpenRouter set.
"""
from __future__ import annotations

import json
from typing import Optional

import httpx

from app.core.config import settings
from packages.schemas import ToolCall, ToolDefinition

from ..base import LLMProvider, LLMResponse


class MistralLLM(LLMProvider):
    name = "mistral"

    def __init__(self, model=None) -> None:
        self.api_key = settings.mistral_api_key
        self.model = model or settings.mistral_model or "mistral-large-latest"
        self.base_url = "https://api.mistral.ai/v1"

    async def complete(
        self,
        messages: list[dict],
        tools: Optional[list[ToolDefinition]] = None,
        temperature: float = 0.3,
        max_tokens: int = 1024,
        response_schema: Optional[object] = None,
    ) -> LLMResponse:
        if not self.api_key:
            raise RuntimeError("MISTRAL_API_KEY not set")

        # 2026-08-09 speed (task #285): mark the system message for
        # Mistral prefix caching.  Same system prompt hits their cache
        # → ~200ms faster + 90% cheaper on the cached-prefix portion.
        # Mistral honors cache_control on the system message.
        cached_messages = []
        for i, m in enumerate(messages):
            if i == 0 and m.get("role") == "system":
                cached_messages.append({**m, "cache_control": {"type": "ephemeral"}})
            else:
                cached_messages.append(m)
        payload: dict = {
            "model": self.model,
            "messages": cached_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = [t.to_openai_format() for t in tools]
            payload["tool_choice"] = "auto"

        if response_schema is not None:
            from .structured_output import openai_response_format
            payload["response_format"] = openai_response_format(
                response_schema, strict=True,
            )

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
            if resp.status_code >= 400:
                # 2026-08-08: log the actual Mistral error body so we can
                # see WHY they 400.  raise_for_status only says "400 Bad
                # Request" with no context, and router treats it as generic
                # failure and cascades — wasting 5-10 sec per call.
                import logging as _l
                _l.getLogger(__name__).warning(
                    "MISTRAL_ERR status=%d model=%s body=%r payload_msgs=%d payload_tools=%s",
                    resp.status_code, self.model, resp.text[:500],
                    len(payload.get("messages", [])),
                    bool(payload.get("tools")),
                )
            resp.raise_for_status()
            data = resp.json()

        choice = data["choices"][0]
        msg = choice["message"]
        tool_calls = []
        for tc in msg.get("tool_calls") or []:
            args_raw = tc["function"]["arguments"]
            args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
            tool_calls.append(ToolCall(id=tc["id"], name=tc["function"]["name"], arguments=args))

        return LLMResponse(
            text=msg.get("content") or "",
            tool_calls=tool_calls,
            finish_reason=choice.get("finish_reason", "stop"),
            raw=data,
        )

    async def stream_complete(
        self,
        messages: list[dict],
        temperature: float = 0.3,
        max_tokens: int = 1024,
    ):
        """2026-08-09 speed sprint (task #283): stream text tokens from
        Mistral as they arrive.  Yields (delta_text, is_final) tuples.

        No tools/response_schema support — this path is ONLY for the
        final plain-text reply after any tool loop has resolved.  The
        caller (twilio_actor) uses this to pipe tokens into
        ElevenLabs stream_synthesize as sentence boundaries land.

        First token arrives ~250-400ms after request instead of waiting
        the full ~1000-1500ms for the complete response."""
        if not self.api_key:
            raise RuntimeError("MISTRAL_API_KEY not set")
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        async with httpx.AsyncClient(timeout=60) as client:
            async with client.stream(
                "POST", f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                },
                json=payload,
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    data_str = line[6:].strip()
                    if data_str == "[DONE]":
                        yield "", True
                        return
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    text = delta.get("content") or ""
                    if text:
                        yield text, False

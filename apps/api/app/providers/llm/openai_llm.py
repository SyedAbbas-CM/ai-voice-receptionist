from __future__ import annotations

import json
from typing import Optional

import httpx

from app.core.config import settings
from packages.schemas import ToolCall, ToolDefinition

from ..base import LLMProvider, LLMResponse


class OpenAILLM(LLMProvider):
    """OpenAI Chat Completions.

    Prompt caching: OpenAI caches prefixes >1024 tokens automatically as long
    as the beginning of the prompt is byte-identical across requests. Our
    brain always sends [system, ...transcript] — the system prompt is at
    position 0 and stable across turns of the same session, so cache hits
    happen automatically from turn 2 onward. No code change needed.
    Verify hits via `usage.prompt_tokens_details.cached_tokens` in the
    response. See https://platform.openai.com/docs/guides/prompt-caching
    """

    name = "openai"

    def __init__(self, model=None) -> None:
        self.api_key = settings.openai_api_key
        self.model = model or settings.openai_model or "gpt-4o-mini"
        self.base_url = "https://api.openai.com/v1"

    async def complete(
        self,
        messages: list[dict],
        tools: Optional[list[ToolDefinition]] = None,
        temperature: float = 0.3,
        max_tokens: int = 1024,
        response_schema: Optional[object] = None,
    ) -> LLMResponse:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY not set")

        # 2026-08-12: GPT-5.x + o-series use max_completion_tokens; older
        # GPT-4.x still requires max_tokens.  Also GPT-5 base / GPT-5.6 luna /
        # o-series require temperature=1 (they reject anything else with 400).
        # GPT-5.4-mini/nano DO allow custom temperature.
        payload: dict = {
            "model": self.model,
            "messages": messages,
        }
        _is_new_family = (
            self.model.startswith("gpt-5")
            or self.model.startswith("o1")
            or self.model.startswith("o3")
            or self.model.startswith("o4")
        )
        # Models that REQUIRE temperature=1 (reject anything else with 400)
        _needs_default_temp = (
            self.model.startswith("o1")
            or self.model.startswith("o3")
            or self.model.startswith("o4")
            or self.model == "gpt-5"
            or self.model.startswith("gpt-5-mini")
            or self.model.startswith("gpt-5-nano")
            or self.model.startswith("gpt-5-pro")
            or self.model.startswith("gpt-5.6-luna")
        )
        if not _needs_default_temp:
            payload["temperature"] = temperature
        if _is_new_family:
            payload["max_completion_tokens"] = max_tokens
        else:
            payload["max_tokens"] = max_tokens
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
        """Task #283: token-level SSE streaming for the FINAL plain-text
        reply.  Yields (delta_text, is_final) tuples.

        No tools / no response_schema — the caller (brain.handle_user_turn)
        only reaches this path after the tool loop already resolved with
        no tool_calls.  Uses the same gpt-5/o-series parameter quirks as
        complete() (max_completion_tokens + default temperature)."""
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY not set")
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "stream": True,
        }
        _is_new_family = (
            self.model.startswith("gpt-5")
            or self.model.startswith("o1")
            or self.model.startswith("o3")
            or self.model.startswith("o4")
        )
        _needs_default_temp = (
            self.model.startswith("o1")
            or self.model.startswith("o3")
            or self.model.startswith("o4")
            or self.model == "gpt-5"
            or self.model.startswith("gpt-5-mini")
            or self.model.startswith("gpt-5-nano")
            or self.model.startswith("gpt-5-pro")
            or self.model.startswith("gpt-5.6-luna")
        )
        if not _needs_default_temp:
            payload["temperature"] = temperature
        if _is_new_family:
            payload["max_completion_tokens"] = max_tokens
        else:
            payload["max_tokens"] = max_tokens

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

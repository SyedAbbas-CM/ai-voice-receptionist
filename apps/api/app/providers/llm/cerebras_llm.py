"""Cerebras Cloud API — OpenAI-compatible.

Docs: https://inference-docs.cerebras.ai/
Notable: ~2000 tokens/second on Llama 3.3 70B and similar. Great for
low-latency voice-agent brain calls where every ms matters.

Free tier as of mid-2026: ~1M tokens/day. Verify at inference.cerebras.ai.
"""
from __future__ import annotations

import json
from typing import Optional

import httpx

from app.core.config import settings
from packages.schemas import ToolCall, ToolDefinition

from ..base import LLMProvider, LLMResponse


class CerebrasLLM(LLMProvider):
    name = "cerebras"

    def __init__(self, model=None) -> None:
        self.api_key = settings.cerebras_api_key
        self.model = model or settings.cerebras_model or "llama-3.3-70b"
        self.base_url = "https://api.cerebras.ai/v1"

    async def complete(
        self,
        messages: list[dict],
        tools: Optional[list[ToolDefinition]] = None,
        temperature: float = 0.3,
        max_tokens: int = 1024,
        response_schema: Optional[object] = None,
    ) -> LLMResponse:
        if not self.api_key:
            raise RuntimeError("CEREBRAS_API_KEY not set")

        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            # 2026-08-11 (task #313): Cerebras gpt-oss-120b REJECTS tool
            # calls without strict mode enabled → router silently cascades
            # to gemma-4-31b (12s cold), adds 1-2s to every turn.  Fix:
            # inject `strict: True` on every function schema AND force
            # `additionalProperties: false` on the parameters object.
            # Confirmed via Cerebras docs: this is the documented way to
            # get reliable tool_calls out of gpt-oss-120b.
            fmt_tools = []
            for t in tools:
                one = t.to_openai_format()
                fn = one["function"]
                fn["strict"] = True
                params = fn.get("parameters") or {}
                if isinstance(params, dict):
                    params.setdefault("additionalProperties", False)
                    # strict mode requires every property named in `required`
                    # AND requires `required` to list every property.
                    props = params.get("properties") or {}
                    if props:
                        params["required"] = list(props.keys())
                    fn["parameters"] = params
                fmt_tools.append(one)
            payload["tools"] = fmt_tools
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

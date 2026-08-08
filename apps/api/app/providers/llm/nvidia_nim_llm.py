"""NVIDIA NIM (build.nvidia.com) API — OpenAI-compatible.

Docs: https://build.nvidia.com and https://docs.nvidia.com/nim/
Free tier: 1000 requests/month via the "build.nvidia.com" API key
(nvapi-...). Great for medium-scale voice-agent testing.

Hosts a bunch of open models via NVIDIA's optimized inference stack — Llama,
Qwen, Nemotron, Mistral. Model IDs are namespaced by provider,
e.g. "meta/llama-3.3-70b-instruct", "nvidia/nemotron-70b", "qwen/qwen-2.5-72b-instruct".
"""
from __future__ import annotations

import json
from typing import Optional

import httpx

from app.core.config import settings
from packages.schemas import ToolCall, ToolDefinition

from ..base import LLMProvider, LLMResponse


class NvidiaNimLLM(LLMProvider):
    name = "nvidia"

    def __init__(self, model=None) -> None:
        self.api_key = settings.nvidia_api_key
        self.model = model or settings.nvidia_model or "meta/llama-3.3-70b-instruct"
        self.base_url = settings.nvidia_base_url or "https://integrate.api.nvidia.com/v1"

    async def complete(
        self,
        messages: list[dict],
        tools: Optional[list[ToolDefinition]] = None,
        temperature: float = 0.3,
        max_tokens: int = 1024,
        response_schema: Optional[object] = None,
    ) -> LLMResponse:
        if not self.api_key:
            raise RuntimeError("NVIDIA_API_KEY not set")

        payload: dict = {
            "model": self.model,
            "messages": messages,
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

        # NIM free tier does GPU cold-start on the first request (~30-90s).
        # Set a generous timeout so the first call doesn't die.
        async with httpx.AsyncClient(timeout=180) as client:
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

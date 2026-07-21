"""OpenRouter — one API for hundreds of models.

Docs: https://openrouter.ai/docs
Notable: model IDs are provider-prefixed like "anthropic/claude-3.5-sonnet",
"google/gemini-2.5-flash", "meta-llama/llama-3.3-70b-instruct". Great fallback
when a specific model isn't available on Groq or Cerebras.

Cost: pay-per-token, no free tier but very cheap for small models.
"""
from __future__ import annotations

import json
from typing import Optional

import httpx

from app.core.config import settings
from packages.schemas import ToolCall, ToolDefinition

from ..base import LLMProvider, LLMResponse


class OpenRouterLLM(LLMProvider):
    name = "openrouter"

    def __init__(self) -> None:
        self.api_key = settings.openrouter_api_key
        self.model = settings.openrouter_model or "meta-llama/llama-3.3-70b-instruct"
        self.base_url = "https://openrouter.ai/api/v1"
        # Optional attribution headers OpenRouter uses for rankings
        self.site_url = settings.openrouter_site_url
        self.app_name = settings.openrouter_app_name or "voiceops-ai-agent"

    async def complete(
        self,
        messages: list[dict],
        tools: Optional[list[ToolDefinition]] = None,
        temperature: float = 0.3,
        max_tokens: int = 1024,
    ) -> LLMResponse:
        if not self.api_key:
            raise RuntimeError("OPENROUTER_API_KEY not set")

        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = [t.to_openai_format() for t in tools]
            payload["tool_choice"] = "auto"

        headers = {"Authorization": f"Bearer {self.api_key}"}
        if self.site_url:
            headers["HTTP-Referer"] = self.site_url
        if self.app_name:
            headers["X-Title"] = self.app_name

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
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

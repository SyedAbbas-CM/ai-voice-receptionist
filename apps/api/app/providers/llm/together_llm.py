"""Together AI LLM provider.  OpenAI-compatible.

Signup: https://api.together.xyz/settings/api-keys (free tier $1 credit +
:free suffix models with no cost).
"""
from __future__ import annotations

import json
import logging
from typing import Optional

import httpx

from app.core.config import settings

from ..base import LLMProvider, LLMResponse
from packages.schemas import ToolDefinition


log = logging.getLogger(__name__)


class TogetherLLM(LLMProvider):
    name = "together"

    def __init__(self, model: Optional[str] = None) -> None:
        self.api_key = getattr(settings, "together_api_key", None) or ""
        self.model = (
            model
            or getattr(settings, "together_model", None)
            or "meta-llama/Llama-3.3-70B-Instruct-Turbo-Free"
        )
        self.base_url = "https://api.together.xyz/v1"

    async def complete(
        self,
        messages: list[dict],
        tools: Optional[list[ToolDefinition]] = None,
        temperature: float = 0.3,
        max_tokens: int = 1024,
        response_schema: Optional[object] = None,
    ) -> LLMResponse:
        if not self.api_key:
            raise RuntimeError("TOGETHER_API_KEY not set")
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if tools:
            payload["tools"] = [
                {"type": "function", "function": t.to_openai_schema()} for t in tools
            ]
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
            r.raise_for_status()
            data = r.json()
        choice = data["choices"][0]
        msg = choice.get("message", {})
        text = msg.get("content") or ""
        tool_calls = None
        if msg.get("tool_calls"):
            tool_calls = [
                {
                    "id": tc.get("id", ""),
                    "name": tc["function"]["name"],
                    "arguments": json.loads(tc["function"].get("arguments", "{}") or "{}"),
                }
                for tc in msg["tool_calls"]
            ]
        return LLMResponse(text=text, tool_calls=tool_calls)

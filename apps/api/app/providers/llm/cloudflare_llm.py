"""Cloudflare Workers AI provider.  OpenAI-compatible endpoint.

Signup: https://dash.cloudflare.com/sign-up  (free, no CC)
Free tier: 10,000 Neurons/day (~1,300 replies typical), completely
separate infra bucket from every other provider we route to.

Uses OpenAI-compat gateway:
    POST https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1/chat/completions

Env:
    CLOUDFLARE_API_TOKEN
    CLOUDFLARE_ACCOUNT_ID
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


class CloudflareLLM(LLMProvider):
    name = "cloudflare"

    def __init__(self, model: Optional[str] = None) -> None:
        self.api_key = getattr(settings, "cloudflare_api_token", None) or ""
        self.account_id = getattr(settings, "cloudflare_account_id", None) or ""
        self.model = (
            model
            or getattr(settings, "cloudflare_model", None)
            or "@cf/meta/llama-3.3-70b-instruct-fp8-fast"
        )
        self.base_url = (
            f"https://api.cloudflare.com/client/v4/accounts/{self.account_id}/ai/v1"
            if self.account_id
            else ""
        )

    async def complete(
        self,
        messages: list[dict],
        tools: Optional[list[ToolDefinition]] = None,
        temperature: float = 0.3,
        max_tokens: int = 1024,
        response_schema: Optional[object] = None,
    ) -> LLMResponse:
        if not self.api_key:
            raise RuntimeError("CLOUDFLARE_API_TOKEN not set")
        if not self.account_id:
            raise RuntimeError("CLOUDFLARE_ACCOUNT_ID not set")
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

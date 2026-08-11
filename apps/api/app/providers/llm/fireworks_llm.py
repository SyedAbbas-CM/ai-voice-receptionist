"""Fireworks AI — OpenAI-compatible inference.

Docs: https://docs.fireworks.ai/api-reference/introduction
Notable: 600 RPM on free tier (20x Groq), $1 signup credit, then PAYG
at ~$0.20/M tokens for llama-3.1-8b.  Function/tool calling supported.

Added 2026-08-11 (task #320) to escape Groq free-tier 30 RPM hell.
Under sustained voice-agent traffic (2-3 LLM calls per caller turn),
Groq exhausts within 60 seconds of talking.  Fireworks 600 RPM =
never hit the ceiling during normal use.

Model IDs use the `accounts/fireworks/models/` prefix.  We default to
llama-v3p3-70b-instruct — fast (sub-500ms TTFT) + Q=4/4 in bench +
tool-calling reliable.  Alternates in router_llm.py for cascade.
"""
from __future__ import annotations

import json
from typing import Optional

import httpx

from app.core.config import settings
from packages.schemas import ToolCall, ToolDefinition

from ..base import LLMProvider, LLMResponse


class FireworksLLM(LLMProvider):
    name = "fireworks"

    def __init__(self, model=None) -> None:
        self.api_key = settings.fireworks_api_key
        # Model format on Fireworks: `accounts/fireworks/models/<id>`
        # Users can pass either the short name (llama-v3p3-70b-instruct)
        # or the fully-qualified accounts/... form; we normalize.
        raw = model or settings.fireworks_model or "llama-v3p3-70b-instruct"
        self.model = raw if raw.startswith("accounts/") else f"accounts/fireworks/models/{raw}"
        self.base_url = "https://api.fireworks.ai/inference/v1"

    async def complete(
        self,
        messages: list[dict],
        tools: Optional[list[ToolDefinition]] = None,
        temperature: float = 0.3,
        max_tokens: int = 1024,
        response_schema: Optional[object] = None,
    ) -> LLMResponse:
        if not self.api_key:
            raise RuntimeError("FIREWORKS_API_KEY not set")

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

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json=payload,
            )
            if resp.status_code >= 400:
                # Log the actual Fireworks error body so we can see WHY it
                # rejected — router treats any 4xx/5xx as failure and
                # cascades otherwise-invisibly.
                import logging as _l
                _l.getLogger(__name__).warning(
                    "FIREWORKS_ERR status=%d model=%s body=%r payload_msgs=%d payload_tools=%s",
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

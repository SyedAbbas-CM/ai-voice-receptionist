from __future__ import annotations

import json
import uuid
from typing import Optional

import httpx

from app.core.config import settings
from packages.schemas import ToolCall, ToolDefinition

from ..base import LLMProvider, LLMResponse


class OllamaLLM(LLMProvider):
    """Local Ollama. Tool calling supported on recent versions (llama3.1, qwen2.5, etc)."""

    name = "ollama"

    def __init__(self) -> None:
        self.base_url = settings.ollama_base_url or "http://localhost:11434"
        self.model = settings.ollama_model or "qwen2.5:7b"

    async def complete(
        self,
        messages: list[dict],
        tools: Optional[list[ToolDefinition]] = None,
        temperature: float = 0.3,
        max_tokens: int = 1024,
    ) -> LLMResponse:
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        if tools:
            payload["tools"] = [t.to_openai_format() for t in tools]

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(f"{self.base_url}/api/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()

        msg = data.get("message") or {}
        tool_calls = []
        for tc in msg.get("tool_calls") or []:
            args = tc["function"].get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"raw": args}
            tool_calls.append(ToolCall(
                id=f"call_{uuid.uuid4().hex[:12]}",
                name=tc["function"]["name"],
                arguments=args or {},
            ))

        return LLMResponse(text=msg.get("content") or "", tool_calls=tool_calls, raw=data)

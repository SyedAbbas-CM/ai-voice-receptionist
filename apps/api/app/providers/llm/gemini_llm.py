from __future__ import annotations

import uuid
from typing import Optional

import httpx

from app.core.config import settings
from packages.schemas import ToolCall, ToolDefinition

from ..base import LLMProvider, LLMResponse


class GeminiLLM(LLMProvider):
    name = "gemini"

    def __init__(self) -> None:
        self.api_key = settings.gemini_api_key
        self.model = settings.gemini_model or "gemini-2.5-flash"
        self.base_url = "https://generativelanguage.googleapis.com/v1beta"

    def _to_gemini_messages(self, messages: list[dict]) -> tuple[list[dict], Optional[str]]:
        system_text = None
        contents = []
        for m in messages:
            role = m["role"]
            if role == "system":
                system_text = (system_text + "\n\n" + m["content"]) if system_text else m["content"]
                continue
            if role == "tool":
                contents.append({
                    "role": "user",
                    "parts": [{"functionResponse": {"name": m.get("name", "tool"), "response": {"content": m["content"]}}}],
                })
                continue
            gemini_role = "model" if role == "assistant" else "user"
            contents.append({"role": gemini_role, "parts": [{"text": m["content"]}]})
        return contents, system_text

    async def complete(
        self,
        messages: list[dict],
        tools: Optional[list[ToolDefinition]] = None,
        temperature: float = 0.3,
        max_tokens: int = 1024,
    ) -> LLMResponse:
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY not set")

        contents, system_text = self._to_gemini_messages(messages)
        payload: dict = {
            "contents": contents,
            "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
        }
        if system_text:
            payload["systemInstruction"] = {"parts": [{"text": system_text}]}
        if tools:
            payload["tools"] = [{
                "functionDeclarations": [
                    {"name": t.name, "description": t.description, "parameters": t.parameters}
                    for t in tools
                ],
            }]

        url = f"{self.base_url}/models/{self.model}:generateContent?key={self.api_key}"
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()

        text_chunks: list[str] = []
        tool_calls: list[ToolCall] = []
        for cand in data.get("candidates") or []:
            for part in (cand.get("content") or {}).get("parts") or []:
                if "text" in part:
                    text_chunks.append(part["text"])
                if "functionCall" in part:
                    fc = part["functionCall"]
                    tool_calls.append(ToolCall(
                        id=f"call_{uuid.uuid4().hex[:12]}",
                        name=fc["name"],
                        arguments=fc.get("args") or {},
                    ))

        return LLMResponse(text="".join(text_chunks), tool_calls=tool_calls, raw=data)

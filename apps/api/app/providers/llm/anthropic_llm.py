from __future__ import annotations

from typing import Optional

import httpx

from app.core.config import settings
from packages.schemas import ToolCall, ToolDefinition

from ..base import LLMProvider, LLMResponse


class AnthropicLLM(LLMProvider):
    """Anthropic Messages API with prompt caching enabled.

    Prompt caching: system prompt + tool defs are marked cache_control ephemeral,
    which lets Anthropic reuse a KV-cache for identical prefixes across turns.
    Cached input tokens cost 10% of normal (Sonnet: $0.30/M cached vs $3/M fresh).
    On a 6-turn voice call with a 2K system prompt, saves ~85% of input token cost
    and cuts TTFT by ~100-300ms per turn from turn 2 onward.

    Cache TTL is 5 minutes — long enough for any single call. If cache misses
    silently (e.g. system prompt drifts between turns), we just pay full price
    for that turn; no functional break.
    """

    name = "anthropic"

    def __init__(self, model=None) -> None:
        self.api_key = settings.anthropic_api_key
        self.model = model or settings.anthropic_model or "claude-sonnet-4-6"
        self.base_url = "https://api.anthropic.com/v1"
        # Enable prompt caching by default; env override for A/B tests
        self.enable_prompt_caching = getattr(
            settings, "anthropic_prompt_caching", True,
        )

    def _split_system(self, messages: list[dict]) -> tuple[Optional[str], list[dict]]:
        """Convert OpenAI-shape messages -> Anthropic-shape.

        Anthropic wants:
          - system prompt outside the messages array
          - tool RESULTS as user messages with {type: tool_result, tool_use_id, content}
          - tool CALLS as assistant messages with {type: tool_use, id, name, input}
            (may be interleaved with {type: text, text: ...} in the same message)

        Both `tool_use_id` and `tool_call_id` are accepted on the input side —
        our schema uses `tool_call_id` (OpenAI-canonical); older code may still
        emit `tool_use_id`.
        """
        import json as _json
        system = None
        rest = []
        for m in messages:
            role = m["role"]
            if role == "system":
                system = (system + "\n\n" + m["content"]) if system else m["content"]
            elif role == "tool":
                rest.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": m.get("tool_call_id") or m.get("tool_use_id") or "tool",
                        "content": m.get("content", ""),
                    }],
                })
            elif role == "assistant" and m.get("tool_calls"):
                blocks: list[dict] = []
                if m.get("content"):
                    blocks.append({"type": "text", "text": m["content"]})
                for tc in m["tool_calls"]:
                    args_raw = tc["function"]["arguments"]
                    args = _json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                    blocks.append({
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": tc["function"]["name"],
                        "input": args,
                    })
                rest.append({"role": "assistant", "content": blocks})
            else:
                rest.append({"role": role, "content": m.get("content", "")})
        return system, rest

    async def complete(
        self,
        messages: list[dict],
        tools: Optional[list[ToolDefinition]] = None,
        temperature: float = 0.3,
        max_tokens: int = 1024,
        response_schema: Optional[object] = None,
    ) -> LLMResponse:
        if not self.api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")

        system, msgs = self._split_system(messages)
        payload: dict = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": msgs,
        }

        # System prompt: send as structured blocks with cache_control on the
        # last block so identical prompts across turns hit cache.
        if system:
            if self.enable_prompt_caching:
                payload["system"] = [{
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }]
            else:
                payload["system"] = system

        # Tool defs: mark the LAST tool with cache_control so the whole tool
        # array + system + any earlier turns all sit in one cache prefix.
        tool_defs: list = []
        if tools:
            tool_defs = [t.to_anthropic_format() for t in tools]

        # 2026-08-07: structured output.  Anthropic doesn't have
        # response_format; the idiomatic way is to define a single
        # tool whose input_schema IS the desired schema, then force
        # tool_choice to that tool.  100% JSON compliance.
        if response_schema is not None:
            from .structured_output import (
                anthropic_tool_for_schema,
                anthropic_tool_choice_for_schema,
            )
            schema_tool = anthropic_tool_for_schema(response_schema)
            if schema_tool is not None:
                tool_defs.append(schema_tool)
                payload["tool_choice"] = anthropic_tool_choice_for_schema(response_schema)

        if tool_defs:
            if self.enable_prompt_caching and tool_defs:
                tool_defs[-1] = {
                    **tool_defs[-1],
                    "cache_control": {"type": "ephemeral"},
                }
            payload["tools"] = tool_defs

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        if self.enable_prompt_caching:
            headers["anthropic-beta"] = "prompt-caching-2024-07-31"

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{self.base_url}/messages",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

        text_chunks: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in data.get("content") or []:
            if block.get("type") == "text":
                text_chunks.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                tool_calls.append(ToolCall(
                    id=block["id"],
                    name=block["name"],
                    arguments=block.get("input") or {},
                ))

        return LLMResponse(
            text="".join(text_chunks),
            tool_calls=tool_calls,
            finish_reason=data.get("stop_reason", "stop"),
            raw=data,
        )

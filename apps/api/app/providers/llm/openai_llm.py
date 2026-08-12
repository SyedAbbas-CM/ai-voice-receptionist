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
        # 2026-08-13 (ChatGPT audit fix): persistent HTTP/2 client.
        # Was creating a fresh httpx.AsyncClient per request → threw away
        # TLS session + TCP + connection pool → 300-600ms extra per call
        # from Pakistan.  Now reused across all complete()/stream_complete()
        # calls for the process lifetime.
        self._client = httpx.AsyncClient(
            http2=True,
            timeout=httpx.Timeout(60.0, connect=5.0),
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
                keepalive_expiry=300.0,
            ),
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

        resp = await self._client.post(
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
        tools: Optional[list[ToolDefinition]] = None,
    ):
        """Task #283 v2: token-level SSE streaming with optional tools.

        Yields (kind, payload, is_final):
          - kind="text",      payload=delta_str,  is_final=bool
          - kind="tool_call", payload=partial_tool_call_dict, is_final=bool

        Detection contract (per OpenAI streaming docs):
          - First "assistant" chunk carries delta.content = null when a
            tool call is coming, delta.content = "" when text is coming.
          - Second chunk carries delta.tool_calls[0].function.name when
            the tool path is confirmed.
        So the caller can peek at the first non-empty yield and know
        which branch the model chose within ~1 RTT + a few ms.
        """
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY not set")
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "stream": True,
        }
        if tools:
            payload["tools"] = [t.to_openai_format() for t in tools]
            payload["tool_choice"] = "auto"
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

        # Accumulate tool_calls across chunks: OpenAI streams them as
        # {index, id, type, function:{name, arguments:""}} in chunk 1,
        # then {index, function:{arguments:"..."}} fragments after.
        tool_calls_acc: dict[int, dict] = {}

        async with self._client.stream(
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
                    # Emit any accumulated tool calls one last time as final.
                    for tc in tool_calls_acc.values():
                        yield "tool_call", tc, True
                    yield "text", "", True
                    return
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                # Text delta
                text = delta.get("content")
                if text:
                    yield "text", text, False
                # Tool-call deltas
                for tc_delta in (delta.get("tool_calls") or []):
                    idx = tc_delta.get("index", 0)
                    acc = tool_calls_acc.setdefault(idx, {
                        "id": None, "name": None, "arguments": "",
                    })
                    if tc_delta.get("id"):
                        acc["id"] = tc_delta["id"]
                    fn = tc_delta.get("function") or {}
                    if fn.get("name"):
                        acc["name"] = fn["name"]
                        # Emit the tool-call announcement as soon as the
                        # name is known — that's the caller's early cancel signal.
                        yield "tool_call", dict(acc), False
                    if "arguments" in fn:
                        acc["arguments"] += fn["arguments"] or ""

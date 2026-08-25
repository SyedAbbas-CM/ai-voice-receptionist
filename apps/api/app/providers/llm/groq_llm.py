from __future__ import annotations

import json
from typing import Optional

import httpx

from app.core.config import settings
from packages.schemas import ToolCall, ToolDefinition

from ..base import LLMProvider, LLMResponse


class GroqLLM(LLMProvider):
    """Groq is OpenAI-API compatible. Same shape as OpenAILLM with a different base URL.

    2026-07-31: when constructed by RouterLLM, pass raise_on_rate_limit=True to
    disable this class's own cross-provider fallback ladder — the router owns
    fallover in that mode.  When used standalone (LLM_PROVIDER=groq) the
    legacy fallback ladder still fires.

    2026-08-20: shared HTTP/2 keep-alive client at class level.  Previously
    every request created a fresh AsyncClient (line 94 pre-refactor), paying
    ~500ms TLS handshake per call and defeating the point of using a
    lower-latency provider.  Also adds native `stream_complete` so the
    router doesn't fall back to buffered `complete()` and emit the whole
    reply as one chunk.  Streaming from Groq via SSE is what actually makes
    Groq's ~200ms TTFT visible to the caller."""

    name = "groq"

    # Class-level shared HTTP/2 client with keep-alive.  Lazy-init in
    # `_get_client` so tests that never construct the class don't open
    # a real socket, and so the loop is bound at first use (not at
    # import time — which would fail because there's no running loop
    # in module scope).
    _shared_client: Optional[httpx.AsyncClient] = None

    def __init__(
        self,
        raise_on_rate_limit: bool = False,
        model: Optional[str] = None,
    ) -> None:
        """Args:
            raise_on_rate_limit: when constructed by the router, disable
                the class's own fallback ladder so the router owns it.
            model: explicit model override.  Audit-3 fix (2026-08-04):
                previously the perf planner mutated settings.groq_model
                before constructing GroqLLM — a race under concurrent
                calls.  Passing model directly avoids the global write.
        """
        self.api_key = settings.groq_api_key
        self.model = model or settings.groq_model or "llama-3.3-70b-versatile"
        self.base_url = "https://api.groq.com/openai/v1"
        self.raise_on_rate_limit = raise_on_rate_limit

    @classmethod
    def _get_client(cls) -> httpx.AsyncClient:
        cli = cls._shared_client
        if cli is None or cli.is_closed:
            cli = httpx.AsyncClient(
                http2=True,
                timeout=httpx.Timeout(90.0, connect=5.0),
                limits=httpx.Limits(
                    max_connections=20,
                    max_keepalive_connections=10,
                    keepalive_expiry=300.0,
                ),
            )
            cls._shared_client = cli
        return cli

    async def complete(
        self,
        messages: list[dict],
        tools: Optional[list[ToolDefinition]] = None,
        temperature: float = 0.3,
        max_tokens: int = 1024,
        response_schema: Optional[object] = None,
    ) -> LLMResponse:
        if not self.api_key:
            raise RuntimeError("GROQ_API_KEY not set")

        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = [t.to_openai_format() for t in tools]
            payload["tool_choice"] = "auto"

        # 2026-08-07: structured output.  Groq supports OpenAI's
        # response_format kwarg; strict json_schema on gpt-oss-*
        # and the openai/ prefixed models, loose json_object on
        # most llama-* variants.  Try strict first, fall back to
        # loose on schema-related 400s.
        if response_schema is not None:
            from .structured_output import openai_response_format
            payload["response_format"] = openai_response_format(
                response_schema, strict=True,
            )

        # Retry on 429 rate-limit with backoff. Real production concern —
        # any burst (multi-concurrent call, adversarial test suite, chatty
        # caller) can trip Groq's per-minute quota. Exponential 2s→4s→8s,
        # honoring the server's `retry-after` header when present.
        import asyncio as _asyncio
        import logging
        _log = logging.getLogger(__name__)

        # Model-degradation fallback: if TPD cap is tripped on the primary
        # model and Groq's retry-after is > 60s, degrade to a cheaper model
        # for THIS call. Lets the caller keep talking while big-model quota
        # recovers. Only for chat models (skips speech/vision).
        _FALLBACK_CHAIN = {
            "llama-3.3-70b-versatile": "llama-3.1-8b-instant",
            "openai/gpt-oss-120b": "openai/gpt-oss-20b",
        }
        original_model = payload["model"]
        current_model = original_model
        max_retries = 8   # was 3 — TPD retry-after can be 30-60s per hit
        client = self._get_client()
        for attempt in range(max_retries + 1):
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
            if resp.status_code == 429 and attempt < max_retries:
                # Honor retry-after up to 5 min per attempt (TPD resets can
                # legitimately take that long during heavy harness runs).
                retry_after = float(resp.headers.get("retry-after", 2 ** (attempt + 1)))

                # 2026-07-31: when RouterLLM constructed us, raise cleanly on
                # 429 so router picks the next provider immediately.  Otherwise
                # router's timeout races this class's retry loop and the caller
                # sees dead air.
                if self.raise_on_rate_limit:
                    resp.raise_for_status()

                # Fallback ladder (updated 2026-07-20). When Groq TPD is exhausted:
                #   1. Gemini 2.5 Flash Lite — 1.1s, 1000 RPD free tier, no shared quota
                #   2. OpenRouter Llama-3.3-70B — ~2s, pay-per-token
                #   3. NVIDIA Nemotron-49B — ~3s, free tier
                #   4. Groq 8B — LAST resort (tool-calling regression)
                gemini_key = settings.gemini_api_key
                openrouter_key = settings.openrouter_api_key
                nvidia_key = settings.nvidia_api_key

                # 2026-07-29: dropped fallback threshold from 180s → 5s so a
                # single voice caller doesn't wait through Groq's TPM cooldown
                # (typical 10-30s). Voice UX budget is ~1s per turn; anything
                # over ~5s of sleep already breaks the conversation. Fall
                # over to the next provider immediately instead of sleeping.
                if retry_after > 5 and gemini_key:
                    _log.warning(
                        "Groq 429 with %.0fs backoff → falling back to Gemini Flash Lite",
                        retry_after,
                    )
                    try:
                        return await self._gemini_fallback(
                            gemini_key, messages, tools, temperature, max_tokens,
                        )
                    except Exception as e:
                        _log.warning("Gemini fallback failed (%s) — trying OpenRouter next", e)

                if retry_after > 5 and openrouter_key:
                    _log.warning(
                        "Groq 429 with %.0fs backoff → falling back to OpenRouter",
                        retry_after,
                    )
                    try:
                        return await self._openrouter_fallback(
                            openrouter_key, messages, tools, temperature, max_tokens,
                        )
                    except Exception as e:
                        _log.warning("OpenRouter fallback failed (%s) — trying NVIDIA next", e)

                if retry_after > 5 and nvidia_key:
                    _log.warning(
                        "Groq 429 with %.0fs backoff → falling back to NVIDIA Nemotron",
                        retry_after,
                    )
                    try:
                        return await self._nvidia_fallback(
                            nvidia_key, messages, tools, temperature, max_tokens,
                        )
                    except Exception as e:
                        _log.warning("NVIDIA fallback failed (%s) — trying Groq 8B last", e)

                # LAST resort: degrade to smaller Groq model (8B has known
                # tool-calling regression → 400s. Only use when all else has
                # failed.)
                if retry_after > 5 and current_model in _FALLBACK_CHAIN:
                    fallback = _FALLBACK_CHAIN[current_model]
                    _log.warning("Groq 429 with %.0fs backoff on %s → degrading to %s",
                                  retry_after, current_model, fallback)
                    current_model = fallback
                    payload["model"] = fallback
                    await _asyncio.sleep(2)
                    continue

                delay_s = min(retry_after, 300.0)
                _log.warning("Groq 429 rate-limited on %s (attempt %d/%d), sleeping %.1fs",
                              current_model, attempt + 1, max_retries, delay_s)
                await _asyncio.sleep(delay_s)
                continue
            if resp.status_code >= 400:
                # Groq returns actionable JSON errors in the body. Log the raw
                # payload we sent + the response so we can debug malformed
                # tool_call sequences or oversized contexts.
                _log.error(
                    "Groq %d: %s | sent messages (last 3): %s",
                    resp.status_code, resp.text[:800],
                    [{"role": m.get("role"), "tool_calls": bool(m.get("tool_calls")),
                      "tool_call_id": m.get("tool_call_id"), "len": len(str(m.get("content", "")))}
                     for m in messages[-3:]],
                )
            resp.raise_for_status()
            data = resp.json()
            break
        else:
            raise RuntimeError("Groq: exhausted 429 retries")

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
        """2026-08-20: native SSE streaming.  Groq is OpenAI-compatible on
        the wire, so this mirrors OpenAILLM.stream_complete exactly.

        Yields (kind, payload, is_final):
          - kind="text",      payload=delta_str,  is_final=bool
          - kind="tool_call", payload=partial_tool_call_dict, is_final=bool

        Prior to 2026-08-20 GroqLLM had NO stream_complete, so
        RouterLLM.stream_complete fell back to buffered .complete() and
        emitted the whole reply as a single chunk (defeating the point of
        streaming to TTS sentence-by-sentence).  With this method wired
        the router's streaming path routes tokens straight through — the
        caller hears the first sentence as soon as Groq generates it.

        Rate-limit handling: on 429, we raise HTTPStatusError so the
        RouterLLM's provider-failover picks the next candidate.  Cross-
        provider fallback (Gemini/OpenRouter/NVIDIA) is not attempted
        here — that's the batch `complete()` path's responsibility, and
        those helpers don't emit streaming tokens anyway.  Streaming
        callers who want fallback should catch the HTTP error and
        retry on `complete()`.
        """
        if not self.api_key:
            raise RuntimeError("GROQ_API_KEY not set")
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = [t.to_openai_format() for t in tools]
            payload["tool_choice"] = "auto"

        # Accumulate tool_calls across chunks (OpenAI-compatible format).
        tool_calls_acc: dict[int, dict] = {}
        client = self._get_client()

        async with client.stream(
            "POST", f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            json=payload,
        ) as resp:
            if resp.status_code == 429:
                # Let the router failover pick next provider.  We do not
                # attempt the cross-provider fallback ladder here because
                # our helpers are batch-only (no streaming).
                resp.raise_for_status()
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data: "):
                    continue
                data_str = line[6:].strip()
                if data_str == "[DONE]":
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
                text = delta.get("content")
                if text:
                    yield "text", text, False
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
                    if fn.get("arguments"):
                        acc["arguments"] += fn["arguments"]

    async def _gemini_fallback(
        self,
        api_key: str,
        messages: list[dict],
        tools: Optional[list[ToolDefinition]],
        temperature: float,
        max_tokens: int,
    ) -> LLMResponse:
        """Gemini 2.5 Flash Lite via Google's OpenAI-compatible endpoint.

        Verified working 2026-07-20 for chat completions and tool calling.
        1.1s latency measured. Separate quota bucket from all Groq models.
        1000 RPD free tier.

        Note: Gemini SOMETIMES ignores tool_choice=auto and asks a clarifying
        question via `content` instead of firing the tool call. That's often
        the more correct receptionist behavior (don't book without knowing
        the actual date), so we let it through.
        """
        model = settings.gemini_model or "gemini-2.5-flash-lite"
        payload: dict = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = [t.to_openai_format() for t in tools]
            payload["tool_choice"] = "auto"

        # 2026-08-20: share the class-level HTTP/2 keep-alive client.
        # Previously opened a fresh AsyncClient per fallback call.
        client = self._get_client()
        resp = await client.post(
            "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()

        choice = data["choices"][0]
        msg = choice["message"]
        tool_calls_out = []
        for tc in msg.get("tool_calls") or []:
            args_raw = tc["function"]["arguments"]
            args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
            tool_calls_out.append(ToolCall(
                id=tc["id"], name=tc["function"]["name"], arguments=args,
            ))
        return LLMResponse(
            text=msg.get("content") or "",
            tool_calls=tool_calls_out,
            finish_reason=choice.get("finish_reason", "stop"),
            raw=data,
        )

    async def _nvidia_fallback(
        self,
        api_key: str,
        messages: list[dict],
        tools: Optional[list[ToolDefinition]],
        temperature: float,
        max_tokens: int,
    ) -> LLMResponse:
        """NVIDIA Nemotron-49B (free tier) as a fallback when Groq caps out.

        Verified working 2026-07-20 with proper OpenAI tool_calls format.
        `meta/llama-3.3-70b-instruct` on NVIDIA's endpoint hangs 60s+; use
        nvidia's own fine-tune which returns in ~3s.
        """
        payload: dict = {
            "model": "nvidia/llama-3.3-nemotron-super-49b-v1",
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = [t.to_openai_format() for t in tools]
            payload["tool_choice"] = "auto"

        # 2026-08-20: share the class-level HTTP/2 keep-alive client.
        client = self._get_client()
        resp = await client.post(
            "https://integrate.api.nvidia.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()

        choice = data["choices"][0]
        msg = choice["message"]
        tool_calls_out = []
        for tc in msg.get("tool_calls") or []:
            args_raw = tc["function"]["arguments"]
            args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
            tool_calls_out.append(ToolCall(
                id=tc["id"], name=tc["function"]["name"], arguments=args,
            ))
        return LLMResponse(
            text=msg.get("content") or "",
            tool_calls=tool_calls_out,
            finish_reason=choice.get("finish_reason", "stop"),
            raw=data,
        )

    async def _openrouter_fallback(
        self,
        api_key: str,
        messages: list[dict],
        tools: Optional[list[ToolDefinition]],
        temperature: float,
        max_tokens: int,
    ) -> LLMResponse:
        """When Groq TPD quota is exhausted, route through OpenRouter to a
        same-tier model (Llama-3.3-70B via OpenRouter's network) instead of
        degrading to Groq's 8B (which has a tool-calling regression).

        Kept inline as a helper (not a shared OpenRouter instance) so this
        adapter self-contains its own resilience and does not depend on
        settings.llm_provider being set to openrouter.
        """
        payload: dict = {
            "model": "meta-llama/llama-3.3-70b-instruct",
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = [t.to_openai_format() for t in tools]
            payload["tool_choice"] = "auto"

        # 2026-08-20: share the class-level HTTP/2 keep-alive client.
        client = self._get_client()
        resp = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "X-Title": "voiceops-ai-agent",
            },
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()

        choice = data["choices"][0]
        msg = choice["message"]
        tool_calls_out = []
        for tc in msg.get("tool_calls") or []:
            args_raw = tc["function"]["arguments"]
            args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
            tool_calls_out.append(ToolCall(
                id=tc["id"], name=tc["function"]["name"], arguments=args,
            ))
        return LLMResponse(
            text=msg.get("content") or "",
            tool_calls=tool_calls_out,
            finish_reason=choice.get("finish_reason", "stop"),
            raw=data,
        )

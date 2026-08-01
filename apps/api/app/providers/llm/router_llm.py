"""LLM router — iterate providers until one works.

Cribbed from the betting-dashboard `llm_pool.py` pattern, adapted to our
LLMProvider interface. Purpose:

  * A single caller call reaches the first healthy provider in a ranked list.
  * Any failure (429, 5xx, timeout, malformed response) drops that provider
    into a short cool-down and immediately retries the next one.
  * When a provider comes back online (cool-down expires + first success),
    it re-enters the rotation at its normal rank.
  * Zero coordination — per-process state, safe to run per-worker.

This wraps the existing per-provider classes (`GroqLLM`, `GeminiLLM`, ...)
so we don't duplicate any API-call code. The factory returns this router
when `LLM_PROVIDER=router` is set.

Env vars:

  LLM_PROVIDER=router
  LLM_ROUTER_ORDER=groq,gemini,nvidia,openrouter
      Comma-separated provider names, tried in order. First one whose API
      key is set AND is not in cool-down wins.
  LLM_ROUTER_COOLDOWN_S=30
      Seconds a provider stays skipped after any failure. Reset on next
      success.
  LLM_ROUTER_TIMEOUT_S=8
      Per-provider timeout before we count it as a failure and try the next.
      Voice UX budget is ~1s per turn, so anything over ~8s of waiting is
      already broken — bail and try elsewhere.

The router is authoritative — it does NOT stack on top of per-provider
retry loops. When LLM_PROVIDER=router, per-provider retries are effectively
bypassed (they'll still exist but the router's timeout fires first).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

from app.core.config import settings
from ..base import LLMProvider, LLMResponse
from packages.schemas import ToolDefinition


log = logging.getLogger(__name__)


DEFAULT_ORDER = "groq,gemini,nvidia,openrouter"
DEFAULT_COOLDOWN_S = 30.0
DEFAULT_TIMEOUT_S = 8.0


def _mk_provider(name: str) -> Optional[LLMProvider]:
    """Lazy-construct one provider by name. Returns None if its API key
    is missing (skip silently — operator hasn't configured it)."""
    try:
        if name == "groq" and settings.groq_api_key:
            from .groq_llm import GroqLLM
            # raise_on_rate_limit=True disables Groq's own fallback ladder so
            # THIS router owns fallover.  Two ladders fighting each other was
            # the root cause of 20-30s dead-air on rate-limits (2026-07-31).
            return GroqLLM(raise_on_rate_limit=True)
        if name == "gemini" and settings.gemini_api_key:
            from .gemini_llm import GeminiLLM
            return GeminiLLM()
        if name in ("nvidia", "nim", "nvidia_nim") and settings.nvidia_api_key:
            from .nvidia_nim_llm import NvidiaNimLLM
            return NvidiaNimLLM()
        if name == "openrouter" and settings.openrouter_api_key:
            from .openrouter_llm import OpenRouterLLM
            return OpenRouterLLM()
        if name == "cerebras" and settings.cerebras_api_key:
            from .cerebras_llm import CerebrasLLM
            return CerebrasLLM()
        if name == "openai" and settings.openai_api_key:
            from .openai_llm import OpenAILLM
            return OpenAILLM()
        if name == "anthropic" and settings.anthropic_api_key:
            from .anthropic_llm import AnthropicLLM
            return AnthropicLLM()
    except Exception as e:
        log.warning("router: provider %s failed to init: %s", name, e)
    return None


class RouterLLM(LLMProvider):
    """Iterates a ranked list of underlying providers.

    State: per-provider `_cool_until` timestamp. On failure, we set it to
    `now + cooldown_s`. On success we clear it. `complete()` walks the
    list in order, skipping any cool-down entries, until one returns.
    """

    name = "router"

    def __init__(self) -> None:
        order_str = os.environ.get("LLM_ROUTER_ORDER", DEFAULT_ORDER)
        self.cooldown_s = float(os.environ.get("LLM_ROUTER_COOLDOWN_S", DEFAULT_COOLDOWN_S))
        self.timeout_s = float(os.environ.get("LLM_ROUTER_TIMEOUT_S", DEFAULT_TIMEOUT_S))

        self.providers: list[tuple[str, LLMProvider]] = []
        for raw in order_str.split(","):
            key = raw.strip().lower()
            if not key:
                continue
            p = _mk_provider(key)
            if p is None:
                log.info("router: skipping %s (no API key set)", key)
                continue
            self.providers.append((key, p))

        if not self.providers:
            raise RuntimeError(
                "RouterLLM initialized with zero usable providers. Set at least "
                "one of GROQ_API_KEY / GEMINI_API_KEY / NVIDIA_API_KEY / OPENROUTER_API_KEY."
            )

        # {provider_name: unix_ts when it becomes usable again}
        self._cool_until: dict[str, float] = {}
        # Model name for observability — pick the first provider's model.
        first_key, first_prov = self.providers[0]
        self.model = f"router({first_key}={getattr(first_prov, 'model', '?')})"
        log.info(
            "router: initialized with %d providers: %s (cooldown=%.0fs, timeout=%.0fs)",
            len(self.providers), [k for k, _ in self.providers],
            self.cooldown_s, self.timeout_s,
        )

    def _available(self, provider_name: str, now: float) -> bool:
        cool = self._cool_until.get(provider_name, 0.0)
        return now >= cool

    def _mark_failed(self, provider_name: str, err: str) -> None:
        self._cool_until[provider_name] = time.time() + self.cooldown_s
        log.warning(
            "router: %s failed (%s), cooling down %.0fs",
            provider_name, err[:120], self.cooldown_s,
        )

    def _mark_ok(self, provider_name: str) -> None:
        self._cool_until.pop(provider_name, None)

    async def complete(
        self,
        messages: list[dict],
        tools: Optional[list[ToolDefinition]] = None,
        temperature: float = 0.3,
        max_tokens: int = 1024,
    ) -> LLMResponse:
        now = time.time()
        errors: list[str] = []
        for name, provider in self.providers:
            if not self._available(name, now):
                cool = self._cool_until[name]
                errors.append(f"{name}=cool_for_{cool - now:.0f}s")
                continue
            try:
                resp = await asyncio.wait_for(
                    provider.complete(messages, tools, temperature, max_tokens),
                    timeout=self.timeout_s,
                )
                # Some providers return empty text + no tool calls on soft-
                # failure (Gemini safety filter, model refuses). Treat as fail
                # so we try the next one.
                if not resp.text and not resp.tool_calls:
                    self._mark_failed(name, "empty response")
                    errors.append(f"{name}=empty")
                    continue
                self._mark_ok(name)
                return resp
            except asyncio.TimeoutError:
                self._mark_failed(name, f"timeout {self.timeout_s}s")
                errors.append(f"{name}=timeout")
            except Exception as e:
                self._mark_failed(name, f"{type(e).__name__}: {e}")
                errors.append(f"{name}={type(e).__name__}")

        raise RuntimeError(
            f"RouterLLM: all providers failed. Attempts: {'; '.join(errors)}"
        )

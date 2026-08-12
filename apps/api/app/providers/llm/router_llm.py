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
import json
import logging
import os
import time
from typing import Optional

from app.core.config import settings
from ..base import LLMProvider, LLMResponse
from packages.schemas import ToolDefinition


log = logging.getLogger(__name__)


# 2026-08-04: expanded default order.  Cerebras + Mistral added after
# discovering they were configured but not routed.  OpenRouter dropped
# from the default (current account has no free-tier access).  Order
# is fastest-first with real-latency fallback per the bench in
# scripts/bench_llms.py.
DEFAULT_ORDER = "groq,cerebras,mistral,gemini,nvidia"
DEFAULT_COOLDOWN_S = 30.0
DEFAULT_TIMEOUT_S = 8.0


def _mk_provider(name: str, model: Optional[str] = None) -> Optional[LLMProvider]:
    """Lazy-construct one provider by name. Returns None if its API key
    is missing (skip silently — operator hasn't configured it).

    2026-08-05: `model` override lets the router try alternate models
    on the SAME provider (Groq rate-limits per-model, so llama-3.3-70b
    429 doesn't affect llama-3.1-8b-instant)."""
    try:
        if name == "groq" and settings.groq_api_key:
            from .groq_llm import GroqLLM
            return GroqLLM(raise_on_rate_limit=True, model=model)
        if name == "gemini" and settings.gemini_api_key:
            from .gemini_llm import GeminiLLM
            return GeminiLLM(model=model) if model else GeminiLLM()
        if name in ("nvidia", "nim", "nvidia_nim") and settings.nvidia_api_key:
            from .nvidia_nim_llm import NvidiaNimLLM
            return NvidiaNimLLM(model=model) if model else NvidiaNimLLM()
        if name == "openrouter" and settings.openrouter_api_key:
            from .openrouter_llm import OpenRouterLLM
            return OpenRouterLLM(model=model) if model else OpenRouterLLM()
        if name == "cerebras" and settings.cerebras_api_key:
            from .cerebras_llm import CerebrasLLM
            return CerebrasLLM(model=model) if model else CerebrasLLM()
        # 2026-08-11 (task #320): Fireworks — 600 RPM free tier (20x Groq)
        if name == "fireworks" and getattr(settings, "fireworks_api_key", None):
            from .fireworks_llm import FireworksLLM
            return FireworksLLM(model=model) if model else FireworksLLM()
        if name == "mistral" and getattr(settings, "mistral_api_key", None):
            from .mistral_llm import MistralLLM
            return MistralLLM(model=model) if model else MistralLLM()
        if name == "openai" and settings.openai_api_key:
            from .openai_llm import OpenAILLM
            return OpenAILLM(model=model) if model else OpenAILLM()
        if name == "anthropic" and settings.anthropic_api_key:
            from .anthropic_llm import AnthropicLLM
            return AnthropicLLM(model=model) if model else AnthropicLLM()
        # 2026-08-06: new providers.  Each has its own env key; router
        # skips them silently when the key is missing.
        if name == "sambanova" and getattr(settings, "sambanova_api_key", None):
            from .sambanova_llm import SambaNovaLLM
            return SambaNovaLLM(model=model) if model else SambaNovaLLM()
        if name == "cloudflare" and getattr(settings, "cloudflare_api_token", None):
            from .cloudflare_llm import CloudflareLLM
            return CloudflareLLM(model=model) if model else CloudflareLLM()
        if name == "together" and getattr(settings, "together_api_key", None):
            from .together_llm import TogetherLLM
            return TogetherLLM(model=model) if model else TogetherLLM()
        if name == "deepseek" and getattr(settings, "deepseek_api_key", None):
            from .deepseek_llm import DeepSeekLLM
            return DeepSeekLLM(model=model) if model else DeepSeekLLM()
    except Exception as e:
        log.warning("router: provider %s failed to init: %s", name, e)
    return None


# 2026-08-07 (rebased against bench 49): per-provider ALTERNATE MODELS.
# Order = fastest + quality-4 FIRST, then fallbacks by descending value.
# When the primary 429s, we try these before cooling the whole provider.
# Rate limits are per-model → more alternates = more capacity buckets.
#
# Bench methodology in docs/rnd-2026-08/50-llm-discovery-playbook.md.
# Re-run `python scripts/bench_all_llms.py` to refresh the ordering.
# Every model ID below verified live via `python -m packages.runtime.llm_discover`.
_PROVIDER_ALTERNATES: dict[str, list[str]] = {
    # Groq: bench-verified order.  llama-3.1-8b-instant (547ms, Q=4/4)
    # beats gpt-oss-20b (566ms, Q=1/4) on both speed AND intelligence.
    # 2026-08-11: alternates=[] on tools calls.  Groq free-tier
    # rate-limits ALL models under one shared bucket — cascading
    # through 6 models on a 429 wastes ~2 sec chasing quota that
    # doesn't exist.  On 429 we drop to next provider (Cerebras).
    "groq": [],
    # Mistral: bench-verified order.  magistral-small tops on latency
    # (569ms, Q=4/4) — surprisingly beats mistral-medium.
    "mistral": [
        "magistral-small-latest",         # 569ms, Q=4/4 — TOP
        "mistral-medium-3.5",             # 800ms, Q=4/4
        "mistral-medium-latest",          # 811ms, Q=4/4 (alias for 3.5)
        "mistral-medium-2508",            # 834ms, Q=4/4
        "ministral-14b-latest",           # 852ms, Q=4/4
        "mistral-large-latest",           # 869ms, Q=2/4
        "mistral-large-2512",             # 916ms, Q=2/4
        "mistral-small-latest",           # 938ms, Q=3/4
        "ministral-8b-latest",            # 764ms, Q=3/4
        "ministral-3b-latest",            # 736ms, Q=3/4
    ],
    # Gemini: 3.5-flash-lite wins on latency (706ms) but Q=2/4.
    # gemini-2.5-flash is Q=3/4 but 1318ms.  Rank by value score.
    "gemini": [
        "gemini-3.5-flash-lite",          # 706ms, Q=2/4 — TOP by value
        "gemini-2.5-flash-lite",          # 773ms, Q=2/4
        "gemini-2.5-flash",               # 1318ms, Q=3/4
        "gemini-3.6-flash",               # 999ms, Q=2/4
        "gemini-3.1-flash-lite",          # 1994ms, Q=3/4
        "gemma-4-26b-a4b-it",             # 1572ms, Q=2/4
        "gemma-4-31b-it",                 # 1594ms, Q=2/4
        "gemini-2.0-flash",               # untested, cheap fallback
        "gemini-2.0-flash-lite",          # untested
    ],
    # Cerebras: 2026-08-11 — env primary is gpt-oss-120b (works with
    # strict tools).  NO alternates — Cerebras free-tier rate-limits
    # ALL models simultaneously, so falling to gemma-4-31b just adds
    # 1s of dead air chasing a shared quota.  On 429 we cascade to
    # Mistral instantly instead.
    "cerebras": [],
    # NVIDIA NIM: 87 chat models — pick the strong general-purpose ones.
    "nvidia": [
        "meta/llama-3.3-70b-instruct",
        "meta/llama-3.1-70b-instruct",
        "meta/llama-3.1-8b-instruct",
        "nvidia/llama-3.1-nemotron-70b-instruct",
        "nvidia/llama-3.3-nemotron-super-49b-v1.5",
        "mistralai/mistral-large-2-instruct",
        "mistralai/mixtral-8x22b-v0.1",
        "google/gemma-4-31b-it",
        "google/gemma-3-12b-it",
        "moonshotai/kimi-k2.6",
        "minimaxai/minimax-m3",
        "deepseek-ai/deepseek-v4-flash",
        "z-ai/glm-5.2",
        "openai/gpt-oss-120b",
    ],
    # OpenRouter: 388 chat models — pick 12 free-tier + cheap paid mix.
    # Preference: free-tier (:free suffix) first so we don't burn credits.
    "openrouter": [
        "openai/gpt-oss-20b:free",
        "google/gemma-4-31b-it:free",
        "google/gemma-4-26b-a4b-it:free",
        "nvidia/nemotron-3-nano-30b-a3b:free",
        "nvidia/nemotron-3-super-120b-a12b:free",
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        "poolside/laguna-s-2.1:free",
        "cohere/north-mini-code:free",
        "meta-llama/llama-3.3-70b-instruct",
        "meta-llama/llama-4-maverick",
        "deepseek/deepseek-v3.2",
        "moonshotai/kimi-k2.6",
    ],
    # SambaNova: added 2026-08-06.  Only free source of Llama-405B (10 RPM).
    "sambanova": [
        "Meta-Llama-3.1-405B-Instruct",
        "Meta-Llama-3.1-70B-Instruct",
        "Meta-Llama-3.1-8B-Instruct",
        "Meta-Llama-3.2-1B-Instruct",
        "Meta-Llama-3.2-3B-Instruct",
        "Meta-Llama-3.3-70B-Instruct",
        "Qwen2.5-72B-Instruct",
        "Qwen2.5-Coder-32B-Instruct",
    ],
    # Cloudflare Workers AI: 10k Neurons/day edge tier.
    "cloudflare": [
        "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
        "@cf/meta/llama-3.1-8b-instruct",
        "@cf/meta/llama-3.2-3b-instruct",
        "@cf/mistralai/mistral-small-3.1-24b-instruct",
        "@cf/qwen/qwen2.5-coder-32b-instruct",
        "@cf/google/gemma-3-12b-it",
    ],
    # DeepSeek: signup no CC, cheap.  Add if key set.
    "deepseek": [
        "deepseek-chat",
        "deepseek-reasoner",
    ],
    # Together AI: signup gives $1 credit + free-tier models.
    "together": [
        "meta-llama/Llama-3.3-70B-Instruct-Turbo-Free",
        "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo",
        "Qwen/Qwen2.5-72B-Instruct-Turbo",
    ],
}


# 2026-08-07: per-model capability metadata.  Populated only for models
# where we KNOW a capability is missing.  Models not listed here are
# assumed capable of everything (default: tools + json_mode + streaming).
# Router skips a model if the current request needs a capability the
# model can't do.
#
# Source: docs/rnd-2026-08/50-llm-discovery-playbook.md §8 + provider docs.
MODEL_CAPABILITIES: dict[str, dict[str, bool]] = {
    # Groq's ALLaM: text-only.  Confirmed via console.groq.com/docs/model/allam-2-7b
    # and model.wiki.  Zero tool_calling/tool_definitions/tool_choice/json_mode.
    # Extremely fast (108ms warm p50) and 4/4 on non-tool English benches
    # — safe as a last-resort speech-act/chitchat fallback only.
    "allam-2-7b": {
        "tool_calling": False,
        "json_mode": False,
        "json_schema_strict": False,
    },
    # Whisper models on Groq — audio input, not chat.  Should never be
    # chosen; list them so probe scripts skip them.
    "whisper-large-v3": {"chat": False, "tool_calling": False, "json_mode": False, "json_schema_strict": False},
    "whisper-large-v3-turbo": {"chat": False, "tool_calling": False, "json_mode": False, "json_schema_strict": False},

    # 2026-08-07: structured-output tiers.  Default (unlisted) = both
    # tool_calling AND json_schema_strict AND json_mode all True.
    # List a model here only when a capability is MISSING.
    #
    # Known loose-only (json_object works, strict json_schema doesn't):
    #   - Groq legacy models before json_schema support (mid-2025 cutoff)
    #   - Mistral open-mistral-nemo (loose only)
    # Add here as we confirm from provider docs.
}


# Capabilities we know about — for autodocumentation only.
SUPPORTED_CAPABILITIES: tuple[str, ...] = (
    "chat",
    "tool_calling",
    "json_mode",              # loose: guarantees valid JSON, not schema
    "json_schema_strict",     # strict: guarantees JSON matches given schema
    "streaming",
)


def _model_supports(model: str, capability: str) -> bool:
    """Return True unless MODEL_CAPABILITIES explicitly says otherwise."""
    caps = MODEL_CAPABILITIES.get(model, {})
    return caps.get(capability, True)


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
        response_schema: Optional[object] = None,
        site: Optional[str] = None,
    ) -> LLMResponse:
        # 2026-08-08: LLM call observability.  `site` is a free-form tag
        # from the caller (e.g. "brain.reply", "extractor", "reactive").
        # We log one line per successful call: site, provider, model,
        # elapsed, and how many attempts we had to burn getting there.
        # No `site` → still logs, just labelled "?".
        _call_started = time.perf_counter()
        now = time.time()
        errors: list[str] = []
        attempts_burned = 0
        for name, provider in self.providers:
            if not self._available(name, now):
                cool = self._cool_until[name]
                errors.append(f"{name}=cool_for_{cool - now:.0f}s")
                continue
            # Build model attempt list: primary first, then alternates.
            # 2026-08-08 v2: TOOL-bearing calls cap at 1 alternate (2
            # attempts total per provider) because tool-schema retries
            # with sibling models on the same provider almost never
            # succeed — the failure was capability, not capacity, and
            # burning more attempts is dead air.  Text-only calls can
            # keep 2 alternates since they usually just hit a temporary
            # rate limit and a sibling helps.
            MAX_ALT = 1 if tools else 2
            attempts: list[tuple[str, LLMProvider]] = [(getattr(provider, "model", "?"), provider)]
            for alt_model in _PROVIDER_ALTERNATES.get(name, [])[:MAX_ALT]:
                alt = _mk_provider(name, model=alt_model)
                if alt is not None:
                    attempts.append((alt_model, alt))
            provider_failed_hard = False
            for model_name, prov in attempts:
                # 2026-08-07: capability gate.  If caller passed tools=[...]
                # but this specific model can't do tool_calling, skip it —
                # otherwise the model would return plain text and quietly
                # break the booking flow.  See MODEL_CAPABILITIES above.
                if tools and not _model_supports(model_name, "tool_calling"):
                    errors.append(f"{name}[{model_name}]=skipped(no_tools)")
                    continue
                # 2026-08-07: structured-output gate.  If caller passed
                # response_schema= but the model can't do even loose
                # json_mode, skip it — the reactive brain can't tolerate
                # freeform text where JSON is expected.
                if response_schema is not None and not _model_supports(model_name, "json_mode"):
                    errors.append(f"{name}[{model_name}]=skipped(no_json)")
                    continue
                try:
                    # Only pass response_schema if the underlying provider's
                    # complete() signature accepts it.  All new adapters do;
                    # older ones fall back to ignoring it (via **kwargs).
                    call_kwargs = {}
                    if response_schema is not None:
                        call_kwargs["response_schema"] = response_schema
                    attempts_burned += 1
                    resp = await asyncio.wait_for(
                        prov.complete(messages, tools, temperature, max_tokens, **call_kwargs),
                        timeout=self.timeout_s,
                    )
                    if not resp.text and not resp.tool_calls:
                        errors.append(f"{name}[{model_name}]=empty")
                        continue  # try next model on same provider
                    self._mark_ok(name)
                    elapsed_ms = (time.perf_counter() - _call_started) * 1000
                    # ONE line per LLM call.  Search "LLM_CALL" in logs.
                    log.info(
                        "LLM_CALL site=%s provider=%s model=%s elapsed_ms=%.0f "
                        "attempts=%d tools=%s json=%s",
                        site or "?", name, model_name, elapsed_ms,
                        attempts_burned,
                        bool(tools), bool(response_schema),
                    )
                    if model_name != getattr(provider, "model", "?"):
                        log.info("router: %s served via fallback model %s",
                                 name, model_name)
                    return resp
                except asyncio.TimeoutError:
                    errors.append(f"{name}[{model_name}]=timeout")
                    provider_failed_hard = True
                    break  # timeout = whole provider is slow, don't try more models
                except Exception as e:
                    msg = str(e)
                    errors.append(f"{name}[{model_name}]={type(e).__name__}")
                    # 2026-08-08: 400 = our request is malformed for THIS
                    # model (e.g. tool-schema drift, unsupported param).
                    # Cooling the whole provider on a 400 is wrong — we
                    # cascade to Groq/rate-limit hell over a client bug.
                    # Treat 400 like model-not-found: try next alternate.
                    is_soft_fail = (
                        "429" in msg
                        or "400" in msg
                        or "not found" in msg.lower()
                        or "does not exist" in msg.lower()
                        or "unsupported" in msg.lower()
                    )
                    if not is_soft_fail:
                        provider_failed_hard = True
                        break
                    # Else keep trying alternates on same provider.
            if provider_failed_hard:
                self._mark_failed(name, errors[-1] if errors else "unknown")
            else:
                # All models exhausted with rate-limit — cool provider.
                self._mark_failed(name, "all models rate-limited")

        raise RuntimeError(
            f"RouterLLM: all providers failed. Attempts: {'; '.join(errors)}"
        )

    async def stream_complete(
        self,
        messages: list[dict],
        temperature: float = 0.3,
        max_tokens: int = 1024,
        tools=None,
    ):
        """Task #283 v2: delegate to the first available provider whose
        model supports the shape asked for.  Yields (kind, payload, is_final).

        - kind="text" → payload is a delta str.
        - kind="tool_call" → payload is a partial tool-call dict.

        If tools=... is passed, we only pick providers whose model has
        tool_calling capability (matches complete()'s gate).  If no
        streaming provider is available, raise NotImplementedError so
        the caller can fall back to batch complete()."""
        now = time.time()
        errors: list[str] = []
        for name, provider in self.providers:
            if not self._available(name, now):
                continue
            model_name = getattr(provider, "model", "?")
            if tools and not _model_supports(model_name, "tool_calling"):
                errors.append(f"{name}[{model_name}]=skipped(no_tools)")
                continue
            base_method = LLMProvider.stream_complete
            prov_method = type(provider).stream_complete
            has_native_stream = prov_method is not base_method
            _call_started = time.perf_counter()
            first_ms: Optional[float] = None
            try:
                if has_native_stream:
                    # True SSE streaming path.
                    async for kind, payload, is_final in provider.stream_complete(
                        messages, temperature=temperature, max_tokens=max_tokens,
                        tools=tools,
                    ):
                        if first_ms is None and (payload or kind == "tool_call"):
                            first_ms = (time.perf_counter() - _call_started) * 1000
                            log.info(
                                "LLM_STREAM_CALL site=brain.reply provider=%s model=%s "
                                "first_kind=%s first_ms=%.0f transport=sse",
                                name, model_name, kind, first_ms,
                            )
                        yield kind, payload, is_final
                        if is_final:
                            self._mark_ok(name)
                            return
                    self._mark_ok(name)
                    return
                # 2026-08-12 FALLBACK: adapter has no stream_complete
                # (cerebras/fireworks/groq/nvidia).  Call batch complete()
                # and emit result as a single "text" chunk so the caller
                # (brain) still gets a working reply — just without the
                # per-token latency win.  Better than skipping the fast
                # provider and defaulting to OpenAI SSE.
                resp = await provider.complete(
                    messages, tools=tools,
                    temperature=temperature, max_tokens=max_tokens,
                )
                elapsed = (time.perf_counter() - _call_started) * 1000
                log.info(
                    "LLM_STREAM_CALL site=brain.reply provider=%s model=%s "
                    "batch_ms=%.0f transport=batch-shim tools=%d",
                    name, model_name, elapsed,
                    len(resp.tool_calls or []),
                )
                # Emit tool_calls first (they take priority over text)
                for tc in (resp.tool_calls or []):
                    yield "tool_call", {
                        "id": tc.id, "name": tc.name,
                        "arguments": json.dumps(tc.arguments) if tc.arguments else "",
                    }, False
                # Then text (may be empty on tool-call turns)
                if resp.text:
                    yield "text", resp.text, False
                yield "text", "", True
                self._mark_ok(name)
                return
            except Exception as e:
                errors.append(f"{name}={type(e).__name__}")
                self._mark_failed(name, str(e))
                continue
        raise NotImplementedError(
            f"RouterLLM: no streaming provider available. Attempts: {'; '.join(errors)}"
        )

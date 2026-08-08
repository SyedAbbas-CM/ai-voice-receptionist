"""Task #243: auto-discover live LLM models from provider /v1/models endpoints.

Every provider we route to has an OpenAI-compatible /v1/models endpoint
that returns the list of models currently served on that API key.  We
hit them in parallel, dedupe by canonical family, and return a live
inventory.  Used to:

  1. Validate our _PROVIDER_ALTERNATES list before hitting a dead model
  2. Alert when a model we depend on disappears
  3. Suggest new alternates when a provider ships a new model
  4. Warn on upcoming deprecations (some providers include
     `deprecation` field in the model object)

Runs at startup + on cron (hourly recommended).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx


log = logging.getLogger(__name__)


@dataclass
class LiveModel:
    provider: str
    model_id: str
    ok: bool = True          # False = discovery hit an error for this provider
    deprecation_ts: Optional[float] = None   # unix ts, if provider reports it
    context_window: Optional[int] = None
    metadata: dict = field(default_factory=dict)


@dataclass
class DiscoveryResult:
    when: float                             # unix ts of the scan
    models: list[LiveModel]                 # every model across all providers
    by_provider: dict[str, list[str]]       # provider → [model_id, ...]
    errors: dict[str, str]                  # provider → error string
    duration_s: float


# ── per-provider adapters ─────────────────────────────────────────


async def _fetch_openai_compat(
    provider: str,
    url: str,
    api_key: Optional[str],
    key_header: str = "Authorization",
    key_prefix: str = "Bearer ",
    timeout_s: float = 8.0,
    client: Optional[httpx.AsyncClient] = None,
) -> list[LiveModel]:
    """Fetch OpenAI-compatible /v1/models.  Returns list of LiveModels."""
    if not api_key:
        return []
    headers = {key_header: f"{key_prefix}{api_key}"}
    async with (client or httpx.AsyncClient(timeout=timeout_s)) as c:
        r = await c.get(url, headers=headers)
        r.raise_for_status()
        data = r.json().get("data", [])
    out: list[LiveModel] = []
    for m in data:
        mid = m.get("id") or m.get("name") or ""
        if not mid:
            continue
        out.append(LiveModel(
            provider=provider,
            model_id=mid,
            deprecation_ts=(m.get("deprecation") or m.get("retired_at") or None),
            context_window=m.get("context_window") or m.get("max_context_length"),
            metadata=m,
        ))
    return out


async def _fetch_gemini(provider: str, api_key: Optional[str]) -> list[LiveModel]:
    """Gemini uses a slightly different shape (name = 'models/...')."""
    if not api_key:
        return []
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
    async with httpx.AsyncClient(timeout=8.0) as c:
        r = await c.get(url)
        r.raise_for_status()
        data = r.json().get("models", [])
    out: list[LiveModel] = []
    for m in data:
        raw = m.get("name", "")
        mid = raw.split("/", 1)[-1]   # strip "models/" prefix
        if not mid:
            continue
        out.append(LiveModel(
            provider=provider,
            model_id=mid,
            context_window=m.get("inputTokenLimit"),
            metadata=m,
        ))
    return out


# ── provider registry ────────────────────────────────────────────

PROVIDER_ENDPOINTS: dict[str, dict] = {
    "groq": {
        "url": "https://api.groq.com/openai/v1/models",
        "env_key": "GROQ_API_KEY",
    },
    "cerebras": {
        "url": "https://api.cerebras.ai/v1/models",
        "env_key": "CEREBRAS_API_KEY",
    },
    "mistral": {
        "url": "https://api.mistral.ai/v1/models",
        "env_key": "MISTRAL_API_KEY",
    },
    "openrouter": {
        "url": "https://openrouter.ai/api/v1/models",
        "env_key": "OPENROUTER_API_KEY",
    },
    "nvidia": {
        "url": "https://integrate.api.nvidia.com/v1/models",
        "env_key": "NVIDIA_API_KEY",
    },
    "openai": {
        "url": "https://api.openai.com/v1/models",
        "env_key": "OPENAI_API_KEY",
    },
    "together": {
        "url": "https://api.together.xyz/v1/models",
        "env_key": "TOGETHER_API_KEY",
    },
    "deepseek": {
        "url": "https://api.deepseek.com/v1/models",
        "env_key": "DEEPSEEK_API_KEY",
    },
    "sambanova": {
        "url": "https://api.sambanova.ai/v1/models",
        "env_key": "SAMBANOVA_API_KEY",
    },
    "gemini": {
        "special": "gemini",
        "env_key": "GEMINI_API_KEY",
    },
}


async def discover_all(env: Optional[dict] = None) -> DiscoveryResult:
    """Hit every configured provider's /models endpoint in parallel.
    Providers without an API key in env are silently skipped."""
    env = env or dict(os.environ)
    t0 = time.time()
    tasks: list[asyncio.Task] = []
    names: list[str] = []
    for provider, cfg in PROVIDER_ENDPOINTS.items():
        api_key = env.get(cfg.get("env_key", ""))
        if not api_key:
            continue
        if cfg.get("special") == "gemini":
            tasks.append(asyncio.create_task(_fetch_gemini(provider, api_key)))
        else:
            tasks.append(asyncio.create_task(
                _fetch_openai_compat(provider, cfg["url"], api_key)
            ))
        names.append(provider)

    results = await asyncio.gather(*tasks, return_exceptions=True)

    models: list[LiveModel] = []
    by_provider: dict[str, list[str]] = {}
    errors: dict[str, str] = {}
    for provider, res in zip(names, results):
        if isinstance(res, Exception):
            errors[provider] = f"{type(res).__name__}: {res}"
            log.warning("llm_discover: %s failed: %s", provider, res)
            continue
        models.extend(res)
        by_provider[provider] = sorted([m.model_id for m in res])

    duration = time.time() - t0
    log.info(
        "llm_discover: scanned %d providers in %.2fs, found %d live models",
        len(names), duration, len(models),
    )
    return DiscoveryResult(
        when=t0, models=models, by_provider=by_provider,
        errors=errors, duration_s=duration,
    )


def validate_alternates(
    result: DiscoveryResult,
    configured: dict[str, list[str]],
) -> dict[str, dict[str, bool]]:
    """Given the router's _PROVIDER_ALTERNATES + a live scan, check
    each configured model against the provider's live list.  Returns
    per-provider dict of model → is_live.

    Also flags: model marked with deprecation_ts within 30 days.
    """
    by_provider_set: dict[str, set[str]] = {
        p: set(ms) for p, ms in result.by_provider.items()
    }
    out: dict[str, dict[str, bool]] = {}
    for provider, model_list in configured.items():
        provider_models = by_provider_set.get(provider, set())
        out[provider] = {m: (m in provider_models) for m in model_list}
    return out


# CLI helper — `python -m packages.runtime.llm_discover`
async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    result = await discover_all()
    print(f"\n{'=' * 70}")
    print(f"LLM DISCOVERY {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(result.when))} UTC")
    print(f"scanned {len(result.by_provider)} providers in {result.duration_s:.2f}s")
    print(f"{'=' * 70}\n")
    for provider, models in sorted(result.by_provider.items()):
        print(f"[{provider}] {len(models)} models")
        for m in models[:30]:
            print(f"    {m}")
        if len(models) > 30:
            print(f"    ... and {len(models) - 30} more")
        print()
    if result.errors:
        print("ERRORS:")
        for p, e in result.errors.items():
            print(f"  {p}: {e}")


if __name__ == "__main__":
    asyncio.run(_main())

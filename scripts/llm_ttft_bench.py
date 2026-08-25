"""LLM TTFT bench (2026-08-20).

Question: what is the actual first-token latency for OpenAI vs
Cerebras vs Groq on the exact system prompt + user message we use
in production?  Groq access may be rate-limited but the LATENCY
number when it does succeed still matters — we can use fast providers
for a burst-mode fastpath and fall back to OpenAI on rate limit.

Prompt sizes tested:
  small:  ~1k token system prompt + short user message
  medium: ~4k token system prompt (typical booking flow)
  full:   actual production system prompt (~10k tokens)

For each (provider, size) combo:
  - 3 runs
  - Report first-token-ms, tokens/sec, total-ms, error rate
  - Show median + min/max

NOT wired into runtime.  Bench only.  Writes results to
docs/llm-ttft-bench-<timestamp>.md.

Usage:
  python3 scripts/llm_ttft_bench.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = REPO_ROOT / "docs"


def _load_env() -> dict[str, str]:
    env_path = REPO_ROOT / ".env"
    env: dict[str, str] = {}
    if not env_path.exists():
        return env
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


ENV = _load_env()


PROMPT_SMALL = (
    "You are a helpful voice-agent receptionist. Reply in ONE short sentence."
)
PROMPT_MEDIUM = (
    "You are Alex, a warm dental receptionist. Reply naturally in 20-30 words "
    "max per turn. Use contractions. Sound curious, not pushy. Never invent "
    "specific times or prices — always call check_availability first. If the "
    "caller mentions a follow-up intent, note it. Keep booking flow tight: "
    "service → date → time → name → phone → confirm. Never say goodbye "
    "before confirming they don't need more help."
)


def _prompt_full() -> str:
    """Try to load the real prompt from packages/core_agent/prompt.py."""
    try:
        sys.path.insert(0, str(REPO_ROOT))
        sys.path.insert(0, str(REPO_ROOT / "apps" / "api"))
        from packages.schemas import BusinessProfile  # type: ignore
        from packages.core_agent.prompt import build_system_prompt  # type: ignore

        biz_path = REPO_ROOT / "sample-data" / "clinic" / "business.json"
        biz = BusinessProfile.model_validate(json.loads(biz_path.read_text()))
        return build_system_prompt(biz)
    except Exception as e:
        print(f"[bench] couldn't load real prompt ({e}), using medium fallback")
        return PROMPT_MEDIUM * 8  # ~10k tokens approx


USER_MSG = "book me a cleaning appointment tomorrow at two thirty."


@dataclass
class RunResult:
    provider: str
    model: str
    size: str
    prompt_chars: int
    first_token_ms: Optional[float]
    total_ms: Optional[float]
    tokens_out: int
    error: Optional[str]


async def _stream_openai_style(
    url: str,
    api_key: str,
    model: str,
    system: str,
    user: str,
    *,
    extra_headers: Optional[dict[str, str]] = None,
    extra_body: Optional[dict[str, Any]] = None,
) -> RunResult:
    """OpenAI Chat Completions SSE shape.  Also works for Cerebras and
    Groq — they all implement OpenAI's API."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": True,
        "max_tokens": 100,
        "temperature": 0.3,
    }
    if extra_body:
        body.update(extra_body)

    first_token: Optional[float] = None
    tokens = 0
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            async with client.stream(
                "POST", url, headers=headers, json=body,
            ) as resp:
                if resp.status_code != 200:
                    txt = await resp.aread()
                    return RunResult(
                        provider="?", model=model, size="?",
                        prompt_chars=len(system),
                        first_token_ms=None, total_ms=None,
                        tokens_out=0,
                        error=f"HTTP {resp.status_code}: {txt[:200]!r}",
                    )
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    data = line[6:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except Exception:
                        continue
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    text = delta.get("content")
                    if text:
                        if first_token is None:
                            first_token = (time.perf_counter() - t0) * 1000
                        tokens += 1
        total = (time.perf_counter() - t0) * 1000
        return RunResult(
            provider="?", model=model, size="?",
            prompt_chars=len(system),
            first_token_ms=first_token, total_ms=total,
            tokens_out=tokens, error=None,
        )
    except Exception as e:
        return RunResult(
            provider="?", model=model, size="?",
            prompt_chars=len(system),
            first_token_ms=None, total_ms=None,
            tokens_out=0,
            error=f"{type(e).__name__}: {e!r}"[:200],
        )


PROVIDERS = [
    {
        "provider": "openai",
        "model": "gpt-4o-mini",
        "url": "https://api.openai.com/v1/chat/completions",
        "key": ENV.get("OPENAI_API_KEY", ""),
    },
    {
        "provider": "openai-fast",
        "model": "gpt-4o-mini",
        "url": "https://api.openai.com/v1/chat/completions",
        "key": ENV.get("OPENAI_API_KEY", ""),
        "extra_body": {"service_tier": "fast"},
    },
    {
        "provider": "cerebras",
        "model": ENV.get("CEREBRAS_MODEL", "llama-3.3-70b"),
        "url": "https://api.cerebras.ai/v1/chat/completions",
        "key": ENV.get("CEREBRAS_API_KEY", ""),
    },
    {
        "provider": "cerebras-oss120b",
        "model": "gpt-oss-120b",
        "url": "https://api.cerebras.ai/v1/chat/completions",
        "key": ENV.get("CEREBRAS_API_KEY", ""),
    },
    {
        "provider": "groq-llama-8b",
        "model": "llama-3.1-8b-instant",
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "key": ENV.get("GROQ_API_KEY", ""),
    },
    {
        "provider": "groq-oss20b",
        "model": "openai/gpt-oss-20b",
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "key": ENV.get("GROQ_API_KEY", ""),
    },
]


async def bench_one(pcfg: dict, size: str, system: str, run_ix: int) -> RunResult:
    if not pcfg["key"]:
        return RunResult(
            provider=pcfg["provider"], model=pcfg["model"], size=size,
            prompt_chars=len(system),
            first_token_ms=None, total_ms=None,
            tokens_out=0,
            error="no api key",
        )
    r = await _stream_openai_style(
        pcfg["url"], pcfg["key"], pcfg["model"], system, USER_MSG,
        extra_body=pcfg.get("extra_body"),
    )
    r.provider = pcfg["provider"]
    r.size = size
    return r


async def main() -> None:
    prompts = {
        "small":  PROMPT_SMALL,
        "medium": PROMPT_MEDIUM,
        "full":   _prompt_full(),
    }
    print(f"[bench] loaded prompts: small={len(prompts['small'])} chars, "
          f"medium={len(prompts['medium'])} chars, "
          f"full={len(prompts['full'])} chars")
    print(f"[bench] user msg: {USER_MSG!r}\n")

    results: list[RunResult] = []
    for size, system in prompts.items():
        for pcfg in PROVIDERS:
            for run_ix in range(3):
                print(f"[bench] {pcfg['provider']:<20} {size:<7} run {run_ix+1}/3 ... ",
                      end="", flush=True)
                r = await bench_one(pcfg, size, system, run_ix)
                results.append(r)
                if r.error:
                    print(f"FAIL ({r.error[:60]})")
                elif r.first_token_ms is None:
                    print(f"NO-TOKENS (total={r.total_ms:.0f}ms tokens={r.tokens_out})")
                else:
                    print(f"first_token={r.first_token_ms:.0f}ms "
                          f"total={r.total_ms:.0f}ms "
                          f"tokens={r.tokens_out}")
                # Small pause between runs to avoid immediate rate limit
                await asyncio.sleep(0.3)

    # Aggregate
    OUT_DIR.mkdir(exist_ok=True, parents=True)
    stamp = time.strftime("%Y-%m-%d_%H%M%S")
    out_path = OUT_DIR / f"llm-ttft-bench-{stamp}.md"
    lines = [
        f"# LLM TTFT bench — {stamp}",
        "",
        "How to read: `first_token` is the number that matters most for perceived speed on voice.  Everything else is total-response time (which also matters but less — we're streaming to TTS sentence-by-sentence).",
        "",
        f"**User message tested:** `{USER_MSG}`",
        "",
        f"**Prompt sizes:** small={len(prompts['small'])} chars, medium={len(prompts['medium'])} chars, full={len(prompts['full'])} chars",
        "",
        "## Summary — median first-token (ms) per provider per size",
        "",
        "| Provider | Model | Small | Medium | Full |",
        "|---|---|---|---|---|",
    ]

    def _median(xs: list[float]) -> Optional[float]:
        xs = sorted([x for x in xs if x is not None])
        if not xs:
            return None
        n = len(xs)
        return xs[n // 2]

    provider_names = list({r.provider for r in results})
    provider_names.sort()

    def _get_model(provider: str) -> str:
        for r in results:
            if r.provider == provider and r.model:
                return r.model
        return "?"

    for prov in provider_names:
        model = _get_model(prov)
        row = f"| {prov} | `{model}` |"
        for size in ("small", "medium", "full"):
            ft = _median([r.first_token_ms for r in results
                          if r.provider == prov and r.size == size and r.first_token_ms is not None])
            errs = [r.error for r in results
                    if r.provider == prov and r.size == size and r.error]
            if ft is not None:
                row += f" **{ft:.0f}ms** |"
            elif errs:
                row += f" ERR |"
            else:
                row += " — |"
        lines.append(row)

    lines += [
        "",
        "## All runs (raw)",
        "",
        "| Provider | Model | Size | Prompt chars | First-token ms | Total ms | Tokens | Error |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        ft = f"{r.first_token_ms:.0f}" if r.first_token_ms is not None else "—"
        tot = f"{r.total_ms:.0f}" if r.total_ms is not None else "—"
        err = r.error or ""
        lines.append(
            f"| {r.provider} | `{r.model}` | {r.size} | {r.prompt_chars} | {ft} | {tot} | {r.tokens_out} | {err[:80]} |"
        )

    lines += [
        "",
        "## What this tells us",
        "",
        "- **Lowest first-token** across sizes = best candidate for fastpath primary",
        "- **Rate limit errors** = provider only usable for burst-mode or with fallback",
        "- **Failure at large prompt** = provider not viable for our real system prompt",
        "- Compare `openai` vs `openai-fast` to see if service_tier=fast is doing anything",
        "",
        "Next: if a fast provider succeeds cleanly, wire it into `RouterLLM` as primary for simple turns (no tool calls) with OpenAI as fallback for rate limit + complex reasoning.",
    ]

    out_path.write_text("\n".join(lines))
    print(f"\n[bench] wrote {out_path.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    asyncio.run(main())

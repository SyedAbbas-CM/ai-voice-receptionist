"""Bench every LLM provider + every model on your account.

Run: python scripts/bench_llms.py

Loads .env, hits each provider with a 1-token 'hi' prompt, reports
status + latency.  Use before choosing router order or perf-planner
model.

Saved 2026-08-04 after discovering the router only used 4 of 6
configured providers.  See docs/rnd-2026-08/40-llm-provider-bench.md
for the resulting router config decision.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

import httpx


def _load_env() -> None:
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        m = re.match(r"^([A-Z_][A-Z0-9_]*)=(.*)$", line.strip())
        if m and not line.strip().startswith("#"):
            os.environ.setdefault(m.group(1), m.group(2).strip())


_load_env()


def bench(url: str, headers: dict, payload: dict, timeout: float = 15.0) -> tuple[str, int]:
    started = time.time()
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.post(url, headers={**headers, "Content-Type": "application/json"}, json=payload)
        ms = int((time.time() - started) * 1000)
        if r.status_code == 200:
            return "OK", ms
        return f"{r.status_code}: {r.text[:100]}", ms
    except Exception as e:
        return f"EXC {type(e).__name__}: {str(e)[:100]}", 0


def _run() -> None:
    print("Bench started; ~15s cap per model.\n")

    groq_key = os.environ.get("GROQ_API_KEY", "")
    if groq_key:
        print("=== GROQ ===")
        for m in ["llama-3.3-70b-versatile", "llama-3.1-8b-instant",
                  "openai/gpt-oss-120b", "openai/gpt-oss-20b", "allam-2-7b"]:
            st, ms = bench(
                "https://api.groq.com/openai/v1/chat/completions",
                {"Authorization": f"Bearer {groq_key}"},
                {"model": m, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5},
            )
            print(f"  {ms:5d}ms  {m:55s}  {st}")

    gem_key = os.environ.get("GEMINI_API_KEY", "")
    if gem_key:
        print("\n=== GEMINI ===")
        for m in ["gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-2.5-pro"]:
            st, ms = bench(
                f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={gem_key}",
                {}, {"contents": [{"parts": [{"text": "hi"}]}]},
            )
            print(f"  {ms:5d}ms  {m:55s}  {st}")

    nv_key = os.environ.get("NVIDIA_API_KEY", "")
    if nv_key:
        print("\n=== NVIDIA ===")
        for m in ["meta/llama-3.1-70b-instruct", "meta/llama-3.1-8b-instruct"]:
            st, ms = bench(
                "https://integrate.api.nvidia.com/v1/chat/completions",
                {"Authorization": f"Bearer {nv_key}"},
                {"model": m, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5},
                timeout=25.0,
            )
            print(f"  {ms:5d}ms  {m:55s}  {st}")

    cb_key = os.environ.get("CEREBRAS_API_KEY", "")
    if cb_key:
        print("\n=== CEREBRAS ===")
        for m in ["gpt-oss-120b"]:
            st, ms = bench(
                "https://api.cerebras.ai/v1/chat/completions",
                {"Authorization": f"Bearer {cb_key}"},
                {"model": m, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5},
            )
            print(f"  {ms:5d}ms  {m:55s}  {st}")

    mt_key = os.environ.get("MISTRAL_API_KEY", "")
    if mt_key:
        print("\n=== MISTRAL ===")
        for m in ["mistral-large-latest", "mistral-small-latest", "mistral-medium-latest",
                  "open-mistral-nemo", "codestral-latest", "ministral-8b-latest",
                  "ministral-3b-latest", "pixtral-12b-2409"]:
            st, ms = bench(
                "https://api.mistral.ai/v1/chat/completions",
                {"Authorization": f"Bearer {mt_key}"},
                {"model": m, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5},
            )
            print(f"  {ms:5d}ms  {m:55s}  {st}")

    lm_endpoint = os.environ.get("LMSTUDIO_ENDPOINT", "http://100.73.8.69:1234")
    print(f"\n=== LM STUDIO ({lm_endpoint}) ===")
    st, ms = bench(
        f"{lm_endpoint}/v1/chat/completions", {},
        {"model": "local-model", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5},
        timeout=5.0,
    )
    print(f"  {ms:5d}ms  local (any loaded model){' ':30s}  {st}")


if __name__ == "__main__":
    _run()

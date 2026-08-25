"""Multi-provider model tournament for the receptionist path.

Same shape as scripts/voice_llm_bench.py but hits EVERY provider we
have keys for (OpenAI, Groq, Cerebras, Fireworks, Mistral, NVIDIA).
Gemini's key is revoked — skipped.

Runs the exact same 7 scenarios from bench 1 against ~2-3 candidate
models per provider. Records:
  - first_any_delta / first_text / first_tool_call latency
  - valid_tool_call vs raw_JSON_leak (leak = disqualifier)
  - p50 / p95 across trials
  - transcript quality preview

CRITICAL: All request bodies use OpenAI-compatible /chat/completions
schema. Groq, Cerebras, Fireworks, Mistral, NVIDIA all support it.
Tools=[{...}] passed through as-is; provider handles or rejects.

Reasoning-family models (gpt-5.x, o-series) auto-swap max_tokens for
max_completion_tokens + reasoning_effort=none.

Run:
  /Users/az/Desktop/Receptionist\\ Agent/.venv/bin/python3 \\
      scripts/voice_llm_bench_multiprovider.py
"""
from __future__ import annotations

import asyncio
import json
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx


REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in (REPO_ROOT / ".env").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    return env


ENV = _load_env()


def _uses_reasoning_params(model: str) -> bool:
    m = model.lower()
    return (
        m.startswith("gpt-5.")
        or m.startswith("o1")
        or m.startswith("o3")
        or m.startswith("o4")
    )


def _build_system_prompt() -> str:
    """Same fallback as bench 1 (app.* imports break the real path)."""
    try:
        import sys as _sys
        _sys.path.insert(0, str(REPO_ROOT))
        from packages.core_agent.prompt import build_system_prompt
        from packages.core_agent.business import BusinessProfile
        with (REPO_ROOT / "sample-data" / "clinic" / "business.json").open() as f:
            biz = BusinessProfile.model_validate(json.load(f))
        return build_system_prompt(biz)
    except Exception as e:
        print(f"[warn] short prompt fallback: {e}")
        return (
            "You are a receptionist at Smile Dental Clinic. Reply naturally "
            "and briefly. When a caller wants to book an appointment or "
            "confirm one, call the appropriate tool."
        )


# Real production tool set — identical to bench 1 for A/B comparability.
TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "emit_semantic_plan",
            "description": (
                "Emit structured intent + facts alongside your natural reply."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": [
                            "propose_action", "confirm_action",
                            "offer_slots", "ask_slot",
                            "acknowledge", "correction",
                        ],
                    },
                    "facts": {"type": "array", "items": {"type": "string"}},
                    "pending_tasks": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["operation"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_availability",
            "description": "Check open appointment slots for a service on a day.",
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {"type": "string"},
                    "date": {"type": "string"},
                },
                "required": ["service"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "book_appointment",
            "description": "Create a booking.",
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {"type": "string"},
                    "start_iso": {"type": "string"},
                    "patient_name": {"type": "string"},
                },
                "required": ["service", "start_iso"],
            },
        },
    },
]


@dataclass
class Scenario:
    key: str
    label: str
    messages: list[dict]


def _scenarios(sp: str) -> list[Scenario]:
    system = {"role": "system", "content": sp}
    return [
        Scenario("hear", '"Can you hear me?"', [
            system,
            {"role": "assistant", "content":
                "Thanks for calling Smile Dental Clinic, how can I help?"},
            {"role": "user", "content": "Hello. Can you hear me?"},
        ]),
        Scenario("services", '"What services?"', [
            system,
            {"role": "assistant", "content":
                "Thanks for calling Smile Dental Clinic, how can I help?"},
            {"role": "user", "content": "What services do you offer?"},
        ]),
        Scenario("yeah_sure", '"Yeah. Sure." (torture)', [
            system,
            {"role": "assistant", "content":
                "Thanks for calling Smile Dental Clinic, how can I help?"},
            {"role": "user", "content":
                "I want a general appointment before implants."},
            {"role": "assistant", "content":
                "Gotcha, we can do a general appointment first. "
                "What day are you hoping for?"},
            {"role": "user", "content": "Latest slot, please."},
            {"role": "assistant", "content":
                "On tomorrow the latest opening is three thirty, "
                "does that work?"},
            {"role": "user", "content": "Yeah. Sure."},
        ]),
        Scenario("slots", '"What slots tomorrow?"', [
            system,
            {"role": "assistant", "content":
                "Thanks for calling Smile Dental Clinic, how can I help?"},
            {"role": "user", "content":
                "What appointment slots are available tomorrow?"},
        ]),
        Scenario("direct_book", '"Book cleaning tomorrow at 10"', [
            system,
            {"role": "assistant", "content":
                "Thanks for calling Smile Dental Clinic, how can I help?"},
            {"role": "user", "content":
                "Book me a general cleaning tomorrow at ten."},
        ]),
    ]


# Provider definitions — endpoint + auth + candidate models to bench.
# All use OpenAI-compatible chat/completions except Gemini (skipped —
# API key revoked at test time).
@dataclass
class Provider:
    key: str            # short id
    name: str           # provider display name
    base_url: str       # ends in /v1
    api_key: str        # bearer
    models: list[str]   # candidate model ids
    # Some providers (Groq) don't support tools on all models. If True,
    # we still send tools=[...] but tolerate a "tool not supported" 400
    # by re-firing tools-less and marking the model as tool_unsupported.
    tools_optional: bool = False


PROVIDERS: list[Provider] = [
    Provider(
        "openai", "OpenAI",
        "https://api.openai.com/v1",
        ENV.get("OPENAI_API_KEY", ""),
        [
            # Baseline + current prod pick
            "gpt-4.1-nano",
            "gpt-4o-mini",
            # Newer, tested earlier this session
            "gpt-5.6-luna",   # reasoning family — bench uses max_completion_tokens
            "gpt-5.6-terra",
        ],
    ),
    Provider(
        "groq", "Groq",
        "https://api.groq.com/openai/v1",
        ENV.get("GROQ_API_KEY", ""),
        [
            # Probe showed 216ms — huge outlier, may not support tools
            "allam-2-7b",
            # 27B thinking model — may reason before answering
            "qwen/qwen3.6-27b",
            # Compound router
            "groq/compound-mini",
        ],
        tools_optional=True,
    ),
    Provider(
        "fireworks", "Fireworks",
        "https://api.fireworks.ai/inference/v1",
        ENV.get("FIREWORKS_API_KEY", ""),
        [
            "accounts/fireworks/models/deepseek-v4-flash-0731",
            "accounts/fireworks/routers/kimi-k3-fast",
            "accounts/fireworks/routers/glm-5p2-fast",
        ],
        tools_optional=True,
    ),
    Provider(
        "mistral", "Mistral",
        "https://api.mistral.ai/v1",
        ENV.get("MISTRAL_API_KEY", ""),
        [
            "ministral-3b-latest",   # 3B — should be fastest
            "ministral-8b-latest",
            "mistral-small-latest",
        ],
    ),
    Provider(
        "nvidia", "NVIDIA NIM",
        "https://integrate.api.nvidia.com/v1",
        ENV.get("NVIDIA_API_KEY", ""),
        [
            "meta/llama-3.1-8b-instruct",
            # 3B model returned HTTP 500 in probe — skip
            # "meta/llama-3.2-3b-instruct",
        ],
        tools_optional=True,
    ),
    # Cerebras skipped — requires payment (HTTP 402 in probe)
    # Gemini skipped — API key revoked
]


TRIALS = 2  # keep tournament fast; single-provider bench used 3


@dataclass
class TrialResult:
    error: Optional[str] = None
    first_any_delta_ms: Optional[float] = None
    first_text_ms: Optional[float] = None
    first_tool_call_ms: Optional[float] = None
    total_stream_ms: Optional[float] = None
    text_chars: int = 0
    tool_calls: list[dict] = field(default_factory=list)
    finish_reason: Optional[str] = None
    raw_content_preview: str = ""
    tool_unsupported: bool = False

    def leaked_tool_json(self) -> bool:
        c = self.raw_content_preview.lstrip()
        if not c.startswith(("{", "[")):
            return False
        return any(k in c for k in (
            '"name"', '"parameters"', '"function"',
            '"tool_calls"', '"arguments"', '"operation"',
        ))

    def valid_tool_call(self) -> bool:
        return bool(self.tool_calls) and all(
            tc.get("function", {}).get("name") for tc in self.tool_calls
        )


async def _one_trial(
    client: httpx.AsyncClient, provider: Provider, model: str,
    scenario: Scenario,
) -> TrialResult:
    r = TrialResult()

    body: dict[str, Any] = {
        "model": model,
        "messages": scenario.messages,
        "stream": True,
        "tools": TOOLS,
    }
    # Groq / Fireworks: some models don't support include_usage on stream_options
    if provider.key not in ("groq", "fireworks", "nvidia"):
        body["stream_options"] = {"include_usage": True}
    if _uses_reasoning_params(model):
        body["max_completion_tokens"] = 300
        body["reasoning_effort"] = "none"
    else:
        body["max_tokens"] = 300
        body["temperature"] = 0.3

    headers = {
        "Authorization": f"Bearer {provider.api_key}",
        "Accept": "text/event-stream",
    }

    tool_calls_acc: dict[int, dict] = {}
    text_chunks: list[str] = []

    t_req = time.perf_counter()

    async def _run(body_to_send: dict) -> Optional[str]:
        """Returns None on success, else an error string."""
        nonlocal r
        try:
            async with client.stream(
                "POST", f"{provider.base_url}/chat/completions",
                headers=headers, json=body_to_send,
            ) as resp:
                if resp.status_code >= 400:
                    _body = await resp.aread()
                    return f"HTTP {resp.status_code}: {_body.decode(errors='replace')[:200]}"
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    data = line[6:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    now_ms = (time.perf_counter() - t_req) * 1000.0
                    delta = choices[0].get("delta") or {}
                    fr = choices[0].get("finish_reason")
                    if fr:
                        r.finish_reason = fr
                    content = delta.get("content")
                    if content:
                        if r.first_any_delta_ms is None:
                            r.first_any_delta_ms = now_ms
                        if r.first_text_ms is None:
                            r.first_text_ms = now_ms
                        text_chunks.append(content)
                    for tc_delta in (delta.get("tool_calls") or []):
                        if r.first_any_delta_ms is None:
                            r.first_any_delta_ms = now_ms
                        if r.first_tool_call_ms is None:
                            r.first_tool_call_ms = now_ms
                        idx = tc_delta.get("index", 0)
                        acc = tool_calls_acc.setdefault(idx, {
                            "id": None,
                            "function": {"name": None, "arguments": ""},
                        })
                        if tc_delta.get("id"):
                            acc["id"] = tc_delta["id"]
                        fn = tc_delta.get("function") or {}
                        if fn.get("name"):
                            acc["function"]["name"] = fn["name"]
                        if "arguments" in fn:
                            acc["function"]["arguments"] += fn.get("arguments") or ""
                return None
        except Exception as e:
            return f"{type(e).__name__}: {e}"

    err = await _run(body)
    if err and provider.tools_optional and "tools" in err.lower():
        # Retry without tools
        body_no_tools = {k: v for k, v in body.items() if k != "tools"}
        r.tool_unsupported = True
        err = await _run(body_no_tools)

    if err:
        r.error = err
        return r

    r.total_stream_ms = (time.perf_counter() - t_req) * 1000.0
    r.text_chars = sum(len(c) for c in text_chunks)
    r.tool_calls = list(tool_calls_acc.values())
    r.raw_content_preview = "".join(text_chunks)[:400]
    return r


def _fmt_ms(v: Optional[float]) -> str:
    return f"{v:.0f}ms" if v is not None else "  -   "


def _median(values: list[Optional[float]]) -> Optional[float]:
    xs = [v for v in values if v is not None]
    if not xs:
        return None
    return statistics.median(xs)


def _p95(values: list[Optional[float]]) -> Optional[float]:
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    return xs[min(len(xs) - 1, int(len(xs) * 0.95))]


async def main() -> None:
    sp = _build_system_prompt()
    print(f"prompt: {len(sp)} chars\n")
    scenarios = _scenarios(sp)

    # rows: results[(provider, model)][scenario_key] = list[TrialResult]
    results: dict[tuple[str, str], dict[str, list[TrialResult]]] = {}

    client = httpx.AsyncClient(http2=True, timeout=httpx.Timeout(60.0, connect=8.0))

    # Warmup — one openai call to prime TLS
    print("warmup...", end=" ", flush=True)
    try:
        await _one_trial(client, PROVIDERS[0], "gpt-4o-mini", scenarios[0])
        print("done.\n")
    except Exception as e:
        print(f"warmup failed (continuing): {e}\n")

    for prov in PROVIDERS:
        if not prov.api_key:
            print(f"=== {prov.name} SKIPPED (no key) ===\n")
            continue
        for model in prov.models:
            key = (prov.key, model)
            results[key] = {s.key: [] for s in scenarios}
            print(f"=== {prov.name} / {model} ===")
            for scen in scenarios:
                trials = []
                for t in range(TRIALS):
                    r = await _one_trial(client, prov, model, scen)
                    trials.append(r)
                    await asyncio.sleep(0.15)
                results[key][scen.key] = trials
                oks = [r for r in trials if r.error is None]
                errs = [r for r in trials if r.error]
                if not oks:
                    print(f"  {scen.key:<14} ALL ERR: {errs[0].error[:80] if errs else '?'}")
                    continue
                fa = _median([r.first_any_delta_ms for r in oks])
                ft = _median([r.first_text_ms for r in oks])
                tc = _median([r.first_tool_call_ms for r in oks])
                v = sum(1 for r in oks if r.valid_tool_call())
                lk = sum(1 for r in oks if r.leaked_tool_json())
                notool = "" if not any(r.tool_unsupported for r in oks) else "  (tool-unsup)"
                print(
                    f"  {scen.key:<14} first_any={_fmt_ms(fa)}  "
                    f"first_text={_fmt_ms(ft)}  first_tool={_fmt_ms(tc)}  "
                    f"tools={v}/{len(oks)}  leaks={lk}/{len(oks)}{notool}"
                )
            print()

    # === Rank matrix ===
    print("=" * 130)
    print("TOURNAMENT SUMMARY (leaks first, then latency ASC)")
    print("=" * 130)
    header = f"{'model':<52} {'leaks':>8} {'p50':>8} {'p95':>8}"
    for s in scenarios:
        header += f"{s.key:>13}"
    print(header)
    rows: list[tuple[float, int, str]] = []
    for (pkey, model), by_scen in results.items():
        all_first: list[Optional[float]] = []
        total_leaks = 0
        total_trials = 0
        row_cells = ""
        any_ok = False
        for scen in scenarios:
            oks = [r for r in by_scen[scen.key] if r.error is None]
            if not oks:
                row_cells += f"{'ERR':>13}"
                continue
            any_ok = True
            firsts = [r.first_any_delta_ms for r in oks if r.first_any_delta_ms is not None]
            p50 = statistics.median(firsts) if firsts else None
            row_cells += f"{_fmt_ms(p50):>13}"
            all_first.extend(r.first_any_delta_ms for r in oks)
            total_leaks += sum(1 for r in oks if r.leaked_tool_json())
            total_trials += len(oks)
        p50_all = _median(all_first)
        p95_all = _p95(all_first)
        prefix = (
            f"{pkey + '/' + model:<52} "
            f"{f'{total_leaks}/{total_trials}':>8} "
            f"{_fmt_ms(p50_all):>8} "
            f"{_fmt_ms(p95_all):>8}"
        )
        sort_p50 = p50_all if p50_all is not None else 9e9
        if not any_ok:
            sort_p50 = 9.9e9
        rows.append((sort_p50, total_leaks, prefix + row_cells))
    rows.sort(key=lambda t: (t[1] > 0, t[0]))
    for _, _, s in rows:
        print(s)

    # === Torture-test focus ===
    print()
    print("=" * 130)
    print("TORTURE TEST — \"Yeah. Sure.\" (semantic-plan / tool decision)")
    print("=" * 130)
    print(f"{'model':<52} {'first_delta':>13} {'tool_valid':>13} {'leaked':>10}  preview")
    for (pkey, model), by_scen in results.items():
        oks = [r for r in by_scen["yeah_sure"] if r.error is None]
        if not oks:
            errs = [r for r in by_scen["yeah_sure"] if r.error]
            print(f"{pkey + '/' + model:<52} {'ERR':>13}  {errs[0].error[:60] if errs else '?'}")
            continue
        fa = _median([r.first_any_delta_ms for r in oks])
        v = sum(1 for r in oks if r.valid_tool_call())
        lk = sum(1 for r in oks if r.leaked_tool_json())
        preview = ""
        for r in oks:
            if r.tool_calls:
                tc0 = r.tool_calls[0]
                nm = tc0.get("function", {}).get("name", "?")
                args = tc0.get("function", {}).get("arguments", "")[:40]
                preview = f"tool={nm} args={args!r}"
                break
            elif r.raw_content_preview:
                preview = f"text={r.raw_content_preview[:80]!r}"
                break
        print(
            f"{pkey + '/' + model:<52} "
            f"{_fmt_ms(fa):>13} "
            f"{f'{v}/{len(oks)}':>13} "
            f"{f'{lk}/{len(oks)}':>10}  {preview[:80]}"
        )

    await client.aclose()

    print()
    print("Interpretation:")
    print("  - Winner = zero leaks + lowest p50")
    print("  - Any leaks disqualifies (safety > speed)")
    print("  - tool_valid < TRIALS on torture test = model can't do semantic decisions")
    print("  - tool-unsup label = model doesn't accept our tool schema")


if __name__ == "__main__":
    asyncio.run(main())

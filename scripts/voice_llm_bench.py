"""Model tournament for the receptionist path.

Question: which OpenAI model minimizes total-time-to-caller-hears while
NOT emitting tool JSON as content?

Same shape as scripts/bench_el_chunk_size.py + bench_flux_encoding.py.
Standalone. Does not touch the running server. Uses production system
prompt + real tool schemas + real OpenAI account.

Scenarios per ChatGPT audit (2026-08-23):
  1. "Can you hear me?"                        conversational TTFT
  2. "What services do you offer?"             direct spoken response
  3. "Yeah. Sure."  (booking context)          tool torture test
  4. "What slots are available tomorrow?"      tool selection
  5. tool result → confirmation                second-round tool latency
  6. Caller correction                         state/instruction following

Candidates per ChatGPT + user:
  gpt-4o-mini              (current baseline — safe control)
  gpt-4.1-nano             (was in the mix earlier this session)
  gpt-5.4-nano             (the failing model, for reference)
  gpt-5.6-luna  eff=none   (ChatGPT #1 — the recommended fast model)
  gpt-5.6-terra eff=none   (ChatGPT for tool-lane)
  gpt-5.6-sol   eff=none   (probe)

Per-model per-scenario:
  first_any_delta  first_text  first_tool_call
  valid_tool_call%  raw_JSON_leak%
  total_stream_ms
  p50 / p95 across trials
  tokens + est cost

Run:
  /Users/az/Desktop/Receptionist\\ Agent/.venv/bin/python3 \\
      scripts/voice_llm_bench.py

Optional args (edit CANDIDATES / SCENARIOS / TRIALS in the file — keep
this script single-shot, no CLI flag creep).
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
API_KEY = ENV.get("OPENAI_API_KEY")

# 2026-08-23: reasoning-family models (gpt-5.x, o-series) don't accept
# max_tokens — they need max_completion_tokens. Detect by prefix.
def _uses_reasoning_params(model: str) -> bool:
    m = model.lower()
    return (
        m.startswith("gpt-5.")
        or m.startswith("o1")
        or m.startswith("o3")
        or m.startswith("o4")
    )


# Real production system prompt (built once, then reused across all
# scenarios so the prefix is identical and cache behavior matches prod).
def _build_system_prompt() -> str:
    """Best-effort — import the real builder if we can, else use a
    representative short receptionist prompt so bench still works.

    We deliberately do NOT force the full production prompt import path
    because that pulls in the app.core.config chain. Instead we invoke
    the pure prompt builder with sample business data."""
    try:
        # This path avoids the app.* imports.
        import sys as _sys
        _sys.path.insert(0, str(REPO_ROOT))
        from packages.core_agent.prompt import build_system_prompt
        from packages.core_agent.business import BusinessProfile
        with (REPO_ROOT / "sample-data" / "clinic" / "business.json").open() as f:
            biz = BusinessProfile.model_validate(json.load(f))
        return build_system_prompt(biz)
    except Exception as e:
        print(f"[warn] falling back to short prompt: {e}")
        return (
            "You are a receptionist at Smile Dental Clinic. "
            "Reply naturally and briefly. When a caller wants to book "
            "an appointment or confirm one, call the appropriate tool."
        )


# Minimal but production-shaped tool set. We mirror the real
# emit_semantic_plan definition + a couple more tools so tool-selection
# accuracy has options.
TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "emit_semantic_plan",
            "description": (
                "Emit structured intent + facts alongside your natural "
                "reply. Use for propose_action, confirm_action, "
                "offer_slots, ask_slot, acknowledge, correction."
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
                    "pending_tasks": {
                        "type": "array", "items": {"type": "string"},
                    },
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
                    "date": {"type": "string", "description": "ISO date"},
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


# ── Scenarios ─────────────────────────────────────────────────────────

@dataclass
class Scenario:
    key: str
    label: str
    messages: list[dict]  # OpenAI-shape chat messages (system prepended later)


def _scenarios(system_prompt: str) -> list[Scenario]:
    sp = {"role": "system", "content": system_prompt}
    return [
        Scenario(
            key="hear",
            label='"Can you hear me?"',
            messages=[
                sp,
                {"role": "assistant", "content":
                    "Thanks for calling Smile Dental Clinic, how can I help?"},
                {"role": "user", "content": "Hello. Can you hear me?"},
            ],
        ),
        Scenario(
            key="services",
            label='"What services do you offer?"',
            messages=[
                sp,
                {"role": "assistant", "content":
                    "Thanks for calling Smile Dental Clinic, how can I help?"},
                {"role": "user", "content": "What services do you offer?"},
            ],
        ),
        Scenario(
            key="yeah_sure",
            label='"Yeah. Sure." in booking context (torture test)',
            messages=[
                sp,
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
            ],
        ),
        Scenario(
            key="slots",
            label='"What slots tomorrow?"',
            messages=[
                sp,
                {"role": "assistant", "content":
                    "Thanks for calling Smile Dental Clinic, how can I help?"},
                {"role": "user", "content":
                    "What appointment slots are available tomorrow?"},
            ],
        ),
        Scenario(
            key="tool_confirm",
            label="tool result → confirmation",
            messages=[
                sp,
                {"role": "user", "content":
                    "Book a general cleaning for tomorrow at three thirty."},
                {
                    "role": "assistant", "content": None,
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "book_appointment",
                            "arguments": json.dumps({
                                "service": "general cleaning",
                                "start_iso": "2026-08-24T15:30:00",
                            }),
                        },
                    }],
                },
                {
                    "role": "tool", "tool_call_id": "call_1",
                    "content": json.dumps({
                        "ok": True,
                        "confirmation": "BK-4821",
                        "starts_at": "2026-08-24 3:30 PM",
                    }),
                },
            ],
        ),
        Scenario(
            key="correction",
            label="caller correction of prior turn",
            messages=[
                sp,
                {"role": "assistant", "content":
                    "Thanks for calling Smile Dental Clinic, how can I help?"},
                {"role": "user", "content":
                    "Book me for Tuesday at two."},
                {"role": "assistant", "content":
                    "Got it, Tuesday at two. Which service?"},
                {"role": "user", "content":
                    "Actually make it Wednesday at three, general cleaning."},
            ],
        ),
        Scenario(
            # Voice-agent addition 2026-08-23: all slots present in one
            # turn. A competent model should fire book_appointment (or
            # check_availability if pedantic) directly. Nano-family
            # models tend to over-clarify — that failure mode costs a
            # whole extra round-trip on real calls.
            key="direct_book",
            label='"Book me a cleaning tomorrow at ten" (direct-book)',
            messages=[
                sp,
                {"role": "assistant", "content":
                    "Thanks for calling Smile Dental Clinic, how can I help?"},
                {"role": "user", "content":
                    "Book me a general cleaning tomorrow at ten."},
            ],
        ),
    ]


# ── Candidates ────────────────────────────────────────────────────────

@dataclass
class Candidate:
    key: str
    model: str
    reasoning_effort: Optional[str] = None  # gpt-5.x only
    service_tier: Optional[str] = None      # "fast" if the account allows

CANDIDATES: list[Candidate] = [
    Candidate("mini",   "gpt-4o-mini"),
    Candidate("nano41", "gpt-4.1-nano"),
    Candidate("nano54", "gpt-5.4-nano"),
    Candidate("luna",   "gpt-5.6-luna",  reasoning_effort="none"),
    Candidate("terra",  "gpt-5.6-terra", reasoning_effort="none"),
    Candidate("sol",    "gpt-5.6-sol",   reasoning_effort="none"),
]

TRIALS = 3


# ── One trial ─────────────────────────────────────────────────────────

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
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    cached_tokens: Optional[int] = None
    raw_content_preview: str = ""  # for leak detection

    def leaked_tool_json(self) -> bool:
        """Did the model emit tool-JSON structure inside content?"""
        c = self.raw_content_preview.lstrip()
        if not c.startswith(("{", "[")):
            return False
        return any(k in c for k in (
            '"name"', '"parameters"', '"function"',
            '"tool_calls"', '"arguments"', '"operation"',
        ))

    def valid_tool_call(self) -> bool:
        """Did the model produce a well-formed tool_calls array?"""
        return bool(self.tool_calls) and all(
            tc.get("function", {}).get("name") for tc in self.tool_calls
        )


async def _one_trial(
    client: httpx.AsyncClient, cand: Candidate, scenario: Scenario,
) -> TrialResult:
    r = TrialResult()

    body: dict[str, Any] = {
        "model": cand.model,
        "messages": scenario.messages,
        "tools": TOOLS,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    # Reasoning models: max_completion_tokens, plus reasoning_effort.
    if _uses_reasoning_params(cand.model):
        body["max_completion_tokens"] = 300
        if cand.reasoning_effort:
            body["reasoning_effort"] = cand.reasoning_effort
    else:
        body["max_tokens"] = 300
        body["temperature"] = 0.3
    if cand.service_tier:
        body["service_tier"] = cand.service_tier

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Accept": "text/event-stream",
    }

    tool_calls_acc: dict[int, dict] = {}
    text_chunks: list[str] = []

    t_req = time.perf_counter()

    try:
        async with client.stream(
            "POST", "https://api.openai.com/v1/chat/completions",
            headers=headers, json=body,
        ) as resp:
            if resp.status_code >= 400:
                _body = await resp.aread()
                r.error = f"HTTP {resp.status_code}: {_body.decode()[:200]}"
                return r
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
                # Usage frame (last chunk when include_usage=true).
                if usage := chunk.get("usage"):
                    r.prompt_tokens = usage.get("prompt_tokens")
                    r.completion_tokens = usage.get("completion_tokens")
                    d = usage.get("prompt_tokens_details") or {}
                    r.cached_tokens = d.get("cached_tokens")
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
    except Exception as e:
        r.error = f"{type(e).__name__}: {e}"
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
    idx = min(len(xs) - 1, int(len(xs) * 0.95))
    return xs[idx]


async def main() -> None:
    if not API_KEY:
        print("FATAL: no OPENAI_API_KEY in .env")
        return

    sp = _build_system_prompt()
    print(f"prompt: {len(sp)} chars")
    scenarios = _scenarios(sp)
    print(f"scenarios: {len(scenarios)}   candidates: {len(CANDIDATES)}   "
          f"trials: {TRIALS}\n")

    client = httpx.AsyncClient(http2=True, timeout=httpx.Timeout(60.0, connect=5.0))
    # Warmup: 1 request to prime TLS + connection pool.
    print("warmup...", flush=True, end=" ")
    _wc = Candidate("warm", "gpt-4o-mini")
    await _one_trial(client, _wc, scenarios[0])
    print("done.\n")

    # Nested results: results[cand.key][scenario.key] = list[TrialResult]
    results: dict[str, dict[str, list[TrialResult]]] = {
        c.key: {s.key: [] for s in scenarios} for c in CANDIDATES
    }

    for cand in CANDIDATES:
        eff = f" eff={cand.reasoning_effort}" if cand.reasoning_effort else ""
        print(f"=== {cand.model}{eff} ===")
        for scen in scenarios:
            for t in range(TRIALS):
                r = await _one_trial(client, cand, scen)
                results[cand.key][scen.key].append(r)
                # Small gap so we don't hammer.
                await asyncio.sleep(0.2)
            # Per-scenario summary line.
            rs = results[cand.key][scen.key]
            oks = [r for r in rs if r.error is None]
            errs = [r for r in rs if r.error]
            if not oks:
                print(f"  {scen.key:<14}  ALL ERR: {errs[0].error[:60] if errs else '?'}")
                continue
            first_any_p50 = _median([r.first_any_delta_ms for r in oks])
            first_text_p50 = _median([r.first_text_ms for r in oks])
            first_tool_p50 = _median([r.first_tool_call_ms for r in oks])
            tool_valid = sum(1 for r in oks if r.valid_tool_call())
            tool_leak = sum(1 for r in oks if r.leaked_tool_json())
            print(
                f"  {scen.key:<14}  first_any={_fmt_ms(first_any_p50)}  "
                f"first_text={_fmt_ms(first_text_p50)}  "
                f"first_tool={_fmt_ms(first_tool_p50)}  "
                f"tool_valid={tool_valid}/{len(oks)}  "
                f"leaked={tool_leak}/{len(oks)}"
            )
        print()

    # ── Overall matrix ────────────────────────────────────────────────
    # Voice-agent 2026-08-23: leak rate comes FIRST. It is the DISQUALIFIER —
    # any model with non-zero leaks fails production regardless of TTFT.
    print("=" * 110)
    print("TOURNAMENT SUMMARY  (leak rate FIRST, then latency)")
    print("=" * 110)
    header = f"{'model':<28}{'leaks':>10}{'ALL p50':>10}{'ALL p95':>10}"
    for s in scenarios:
        header += f"{s.key:>12}"
    print(header)
    # Sort rows: zero-leak first, then by ALL p50 ascending.
    row_data: list[tuple[float, int, str]] = []  # (p50_all, leaks, row_str)
    for cand in CANDIDATES:
        row_scenarios = ""
        all_first: list[Optional[float]] = []
        total_leaks = 0
        total_trials = 0
        any_error_only = True
        for scen in scenarios:
            rs = results[cand.key][scen.key]
            oks = [r for r in rs if r.error is None]
            if not oks:
                row_scenarios += f"{'ERR':>12}"
                continue
            any_error_only = False
            firsts = [r.first_any_delta_ms for r in oks if r.first_any_delta_ms is not None]
            p50 = statistics.median(firsts) if firsts else None
            row_scenarios += f"{_fmt_ms(p50):>12}"
            all_first.extend(r.first_any_delta_ms for r in oks)
            total_leaks += sum(1 for r in oks if r.leaked_tool_json())
            total_trials += len(oks)
        p50_all = _median(all_first)
        p95_all = _p95(all_first)
        prefix = (
            f"{cand.model:<28}"
            f"{f'{total_leaks}/{total_trials}':>10}"
            f"{_fmt_ms(p50_all):>10}"
            f"{_fmt_ms(p95_all):>10}"
        )
        sort_p50 = p50_all if p50_all is not None else 999999.0
        # Any-error models sort last.
        if any_error_only:
            sort_p50 = 9999999.0
        row_data.append((sort_p50, total_leaks, prefix + row_scenarios))
    # zero-leak first, then p50 ascending
    row_data.sort(key=lambda t: (t[1] > 0, t[0]))
    for _, _, s in row_data:
        print(s)

    # ── Torture-test focus ────────────────────────────────────────────
    print()
    print("=" * 100)
    print("TORTURE TEST — \"Yeah. Sure.\" (the CAd26f39 leak scenario)")
    print("=" * 100)
    print(f"{'model':<28}{'first_delta_p50':>18}{'first_delta_p95':>18}"
          f"{'total_p50':>12}{'tool_valid':>13}{'leaked':>10}")
    for cand in CANDIDATES:
        rs = results[cand.key]["yeah_sure"]
        oks = [r for r in rs if r.error is None]
        if not oks:
            errs = [r for r in rs if r.error]
            print(f"{cand.model:<28}  ALL ERR: {errs[0].error[:60] if errs else '?'}")
            continue
        fa = [r.first_any_delta_ms for r in oks if r.first_any_delta_ms is not None]
        ts = [r.total_stream_ms for r in oks if r.total_stream_ms is not None]
        tv = sum(1 for r in oks if r.valid_tool_call())
        tl = sum(1 for r in oks if r.leaked_tool_json())
        print(
            f"{cand.model:<28}"
            f"{_fmt_ms(statistics.median(fa)) if fa else '-':>18}"
            f"{_fmt_ms(_p95(fa)):>18}"
            f"{_fmt_ms(statistics.median(ts)) if ts else '-':>12}"
            f"{f'{tv}/{len(oks)}':>13}"
            f"{f'{tl}/{len(oks)}':>10}"
        )
        # Show one raw preview for each candidate on the torture test.
        for r in oks[:1]:
            print(f"    preview: {r.raw_content_preview[:120]!r}")
            if r.tool_calls:
                for tc in r.tool_calls:
                    print(f"    tool: name={tc.get('function', {}).get('name')} "
                          f"args={tc.get('function', {}).get('arguments', '')[:80]!r}")
    print()

    await client.aclose()

    print("Interpretation:")
    print("  - Rank by lowest ALL p95 (worst-case matters more than median for voice)")
    print("  - Any leaks > 0 disqualifies for production — model can't be trusted.")
    print("  - For booking turns, prefer models where tool_valid == trials on torture test.")
    print("  - If gpt-5.6-luna wins latency but reasoning models need adapter fix, ")
    print("    we'll ship the max_completion_tokens change in openai_llm.py before deploying.")


if __name__ == "__main__":
    asyncio.run(main())

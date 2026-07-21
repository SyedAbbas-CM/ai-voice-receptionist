"""Pytest wrapper for the adversarial scenarios.

Parametrized over every JSONL file in ./scenarios/. One test per scenario.
Skipped by default; run with --run-adversarial.

    pytest apps/api/tests/adversarial/test_adversarial.py --run-adversarial

A summary Markdown is written to reports/{timestamp}_summary.md after the run.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from .harness import (
    Scenario,
    ScenarioResult,
    format_summary_md,
    load_scenarios,
    run_scenario,
)


SCENARIOS_DIR = Path(__file__).parent / "scenarios"
REPORTS_DIR = Path(__file__).parent / "reports"


def _all_scenarios() -> list[tuple[str, Scenario]]:
    """Load every scenario across every JSONL file, return list of (file_stem, scenario)."""
    out: list[tuple[str, Scenario]] = []
    for jsonl in sorted(SCENARIOS_DIR.glob("*.jsonl")):
        for s in load_scenarios(jsonl):
            out.append((jsonl.stem, s))
    return out


ALL = _all_scenarios()
IDS = [f"{stem}/{s.scenario_id}" for stem, s in ALL]


@pytest.fixture(scope="session")
def _report_bucket():
    """Session-scoped list results accumulate into, then written after all tests."""
    results: list[ScenarioResult] = []
    yield results
    if not results:
        return
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.utcnow().strftime("%Y-%m-%d_%H%M%S")
    md_path = REPORTS_DIR / f"{stamp}_summary.md"
    md_path.write_text(format_summary_md(results))
    json_path = REPORTS_DIR / f"{stamp}_results.json"
    json_path.write_text(json.dumps([
        {
            "scenario_id": r.scenario_id, "persona": r.persona, "goal": r.goal,
            "overall": r.overall, "hard_fail": r.hard_fail, "elapsed_s": r.elapsed_s,
            "scores": r.scores, "error": r.error,
            "transcript": [{"role": t.role, "text": t.text, "metadata": t.metadata}
                           for t in r.transcript],
        }
        for r in results
    ], indent=2))
    print(f"\n\n[adversarial] wrote {md_path}")
    print(f"[adversarial] wrote {json_path}")


def _server_reachable() -> bool:
    try:
        with httpx.Client(timeout=3) as c:
            r = c.get(os.environ.get("HARNESS_RECEPTIONIST_URL", "http://127.0.0.1:8001") + "/health")
            return r.status_code == 200
    except Exception:
        return False


import time as _time
_last_run_ended = [0.0]  # mutable module-level to persist across parametrized cases
INTER_SCENARIO_COOLDOWN_S = 3.0  # be nice to Groq's per-minute quota


@pytest.mark.parametrize("scenario_file,scenario", ALL, ids=IDS)
def test_adversarial_scenario(scenario_file: str, scenario: Scenario, _report_bucket):
    """Run one scenario end-to-end. Judged pass/fail by LLM judge.

    NOTE: this asserts overall != 'fail'. Any hard-fail (hallucination,
    inappropriate refusal, missed emergency) → test fails.
    """
    if not _server_reachable():
        pytest.skip("receptionist server not reachable on 8001 — start it first")
    # harness.py loads .env at import time, so GROQ_API_KEY should be visible now
    from .harness import GROQ_KEY
    if not GROQ_KEY:
        pytest.skip("GROQ_API_KEY not set — needed for caller and judge LLMs")

    # Cooldown between scenarios so we don't stack requests against Groq's
    # per-minute quota. Each scenario is ~10-20 LLM calls (caller × turns +
    # judge). 3s gap is enough to spread them out.
    elapsed_since_last = _time.time() - _last_run_ended[0]
    if elapsed_since_last < INTER_SCENARIO_COOLDOWN_S:
        _time.sleep(INTER_SCENARIO_COOLDOWN_S - elapsed_since_last)

    result = asyncio.run(run_scenario(scenario))
    _last_run_ended[0] = _time.time()
    _report_bucket.append(result)

    # Log a compact summary line always
    s = result.scores
    print(
        f"\n  [{scenario.scenario_id}] {result.overall} "
        f"({result.elapsed_s:.1f}s) "
        f"task={s.get('task_success', 0)} tone={s.get('tone_match', 0)} "
        f"refused={s.get('refused_appropriately', 0)} "
        f"hallucinated={s.get('hallucinated', 0)}"
    )
    if result.error:
        print(f"  ERROR: {result.error}")
    if result.hard_fail:
        print(f"  HARD-FAIL: {s.get('hard_fail_reasons')}")
        print(f"  notes: {s.get('notes', '')[:200]}")

    assert result.overall != "fail", (
        f"scenario {scenario.scenario_id} failed. "
        f"hard_fail={result.hard_fail} reasons={s.get('hard_fail_reasons')} "
        f"notes={s.get('notes', '')[:200]}"
    )

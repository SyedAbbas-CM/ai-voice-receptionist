# Post-Reset Launch Checklist

**When to use:** first thing next session after usage quota resets. Zero-orientation restart. Do these before anything else — they save 30-60 min of "where were we."

**Last updated:** 2026-08-14 (end of overdrive + reorg session).

---

## The 5-minute restart

Run these three, in order. All harmless.

### 1. Confirm you're on the right branch + no drift

```bash
cd "/Users/az/Desktop/Receptionist Agent"
git branch --show-current    # must print: feat/architectural-networking
git log --oneline -5         # top commit must be: edacdec merge: SOAK test harness
git status --short           # only .github/ should show as untracked
```

If any of these disagree, STOP. Something moved between sessions. Investigate before touching code.

### 2. Boot smoke — every ratified module imports clean

```bash
cd apps/api
python -c "
import sys
sys.path.insert(0, '.'); sys.path.insert(0, '../..')
from app.routes import twilio, twilio_actor
from packages.observability import arrival_events
from packages.core_agent import speech_commit_gate
from packages.slot_parsers import StructuredInputSession, parse_phone
print('boot smoke OK')
"
```

Expected: `boot smoke OK`. Anything else = imports broken; fix before continuing.

### 3. Full test baseline

```bash
python -m pytest tests/ -q --ignore=tests/adversarial --timeout=30 2>&1 | tail -3
```

Expected: `19 failed, 1171 passed`. If passed drops below 1171, we regressed. If failed grows above 19, we regressed. The 19 pre-existing failures are documented — do not chase them without a specific reason.

---

## Then: where are we?

Read these three, in order. Total ~10 min.

1. **`docs/rnd-2026-08/58-status-and-phase-map-2026-08-14.md`** — current authoritative entry point. What shipped, what's on branches, phase map.
2. **`docs/rnd-2026-08/59-phase0-validation-plan.md`** — the 5-box sanity gate. Everything downstream blocks on this.
3. **`~/.claude/projects/-Users-az-Desktop-Receptionist-Agent/memory/reliability-shipped-2026-08-14.md`** — reference card of ratified additions (SpeechCommitGate, one-gen lock, phone precondition) that MUST survive future refactors.

---

## First substantive move: Phase 0 validation

The reorg session set up everything for Phase 0. Concrete first actions:

### If you have testers available (fastest path to green sanity gate)

1. Start the server: `./apps/api/scripts/run_server.sh`
2. Verify arrival events fire: hit `POST /twilio/voice` locally, grep uvicorn log for `ARRIVAL kind=VOICE_WEBHOOK_RECEIVED`. If not visible, R7 wiring broke — investigate before dialing.
3. Dial in from Karachi. Run through SOAK scenarios (`docs/soak/scenarios.md`) — 3 calls per scenario ≈ 24 total.
4. For each call, run `apps/api/scripts/verify-call.sh CA<sid>`. Log the scorecard.
5. Exit criterion: G1 = 20+ calls, every scenario has ≥2 greens, zero reds across the last 10.

### If you're solo (slower but doable)

1. Same server start.
2. Skip live testers → use recorded audio via `apps/api/scripts/replay-audio.py` against recorded WAVs in `apps/api/data/soak_fixtures/` (see `docs/soak/fixtures-README.md` for what to record if empty).
3. Same verify-call.sh scoring.
4. Live dial ONE call yourself to close G3 (PK booking end-to-end) — can't fake this.

### For G4 (OpenAI Realtime cost)

Independent of soak calls. Follow `docs/rnd-2026-08/59-phase0-validation-plan.md#G4` — 1 five-minute Realtime call with cost recorded to `docs/rnd-2026-08/60-realtime-cost-benchmark.md`. Time-box 2 hours.

### For G5 (client-first vs architecture-first)

Independent. Write `docs/decisions/2026-08-14-client-vs-architecture.md` with the two-choice framing from `59-phase0-validation-plan.md#G5`. That's the whole task — no code.

---

## What NOT to do post-reset

- **Don't merge `feat/r6-prebuffer-backpressure` or `feat/phase2-kernel-wire` yet.** Both wait until Phase 0 base soak validates the unmerged base. Merging early hides regressions.
- **Don't start any plan doc under `docs/superpowers/plans/2026-08-14-*.md` until Phase 0 gate is closed.** The plans are intentionally sequenced.
- **Don't chase yellow scorecards on SOAK scenarios 6 or 7** — full pass markers depend on PHASE2 sub-phase 2c code that isn't shipped yet. Yellow is expected on those.
- **Don't ask ChatGPT to re-audit** without a specific question. The zip you sent covers the reorg; wait for concrete failure data from Phase 0 soak before another audit round.

---

## Reference: branch state at end of overdrive session

| Branch | Head | Purpose | Merge state |
|---|---|---|---|
| `main` | old | Untouched | — |
| `feat/architectural-networking` | `f863d5a` | R1-R5 + R3 + gate + lock + phone precondition + R7 + SOAK + PROJECT-LAUNCH-CHECKLIST | This is the working base. All ratified reliability code lives here. |
| `feat/r6-prebuffer-backpressure` | `36e9652` | AudioPipeline module + 10 tests | HOLD — do not merge until Phase 0 soak proves base is stable without it |
| `feat/phase2-kernel-wire` | `6ffaa70` | Kernel shadow observer scaffold | HOLD — merge only after Phase 0 baseline established |

(Branches `feat/r7-arrival-observability` and `chore/soak-harness` were fully merged and safely deleted with `git branch -d` at end of session — their content lives on base at commits `7888385` and `edacdec`.)

---

## If usage is tight again next session

- **Priority 1:** run the 3 restart commands + read doc #58. If those cost <5%, you can still do soak calls (which don't consume usage — they're on the phone).
- **Priority 2:** the actual real-call soak (Phase 0 G1-G3). Dialing calls is free.
- **Priority 3:** write G5 decision doc. Pure writing.
- **Skip if tight:** G4 Realtime cost benchmark — that's an API call marathon; wait for a full session.

Usage burns fast when I'm generating code. Usage barely moves when you're dialing calls and I'm reading per-call logs to score them. The soak phase is the RIGHT time to be low-quota.

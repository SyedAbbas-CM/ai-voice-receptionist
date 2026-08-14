# PHASE2 sub-phase 2a — Shadow Ratification

**Goal:** run the DialogueKernel in shadow mode on real traffic; watch `KERNEL_SHADOW DIVERGENCE` counts trend to zero; only then flip 2b.

**Position in stack:** first user-affecting bit of doc #56's Phase 2. The scaffold already exists on branch `feat/phase2-kernel-wire` (commit `6ffaa70`). This plan is about MERGING and RUNNING it, not writing new code.

## Prereqs (blocking)
- Phase 0 sanity gate closed (doc `59-phase0-validation-plan.md`)
- Phase 1 winner declared (`docs/rnd-2026-08/61-latency-lab-results.md`)
- The shadow branch merges cleanly onto post-Phase-0 base

## Global Constraints
- **Zero user-visible behavior change.** Any user-noticeable change means the scaffold has a bug — revert immediately.
- **`dialogue_kernel_enabled=False`, `dialogue_kernel_shadow=True`** across the entire 2a window.
- **Divergences MUST be investigated.** Log volume alone is not the goal — every `KERNEL_SHADOW DIVERGENCE` line is a mismatch between the legacy path and the kernel's hypothesis. High divergence rate means the kernel's model of reality is wrong, not that the legacy path is wrong.

## Tasks

### Task 1: Merge scaffold + baseline soak
- [ ] Merge `feat/phase2-kernel-wire` into `feat/architectural-networking`
- [ ] Flip `dialogue_kernel_shadow=true` in `.env`
- [ ] Restart server; verify boot smoke + arrival trail
- [ ] Run 10 real calls (any content); grep uvicorn log for `KERNEL_SHADOW` lines
- [ ] Baseline: expect ~5-10 `KERNEL_SHADOW USER_TURN` per call, similar `TOOL_CALL` counts if any tools fired
- [ ] Commit: shadow flag in `.env.example`

### Task 2: Divergence log analyzer
- [ ] Create `apps/api/scripts/shadow-divergence-report.py`
- [ ] Read a uvicorn log (or stdin), extract `KERNEL_SHADOW DIVERGENCE` lines, group by `at=` field, print counts
- [ ] Test on synthetic input
- [ ] Commit

### Task 3: Widen shadow coverage
Currently only `observed_user_turn` and `observed_tool_call` fire. Wire the remaining hooks:
- [ ] `slot_observed` at every point the extractor writes a field
- [ ] `commit_would_gate` inside `_reply_lies_about_booking` and `_reply_promises_wait_without_tool`
- [ ] `divergence` at each decision point where the kernel's hypothesis differs from the legacy choice (needs Task 4's hypothesis stub first)
- [ ] Tests for each new hook
- [ ] Commit

### Task 4: Kernel dispatch hypothesis (fills `kernel_would_dispatch=-`)
- [ ] Read `packages/core_agent/kernel_wiring.py` to understand its TaskKind mapping
- [ ] Extract "what tool would the kernel dispatch given this turn's intent + slot state?" into a pure function
- [ ] Wire it into `observed_tool_call`
- [ ] Test with 5 canned turn transcripts + expected kernel hypotheses
- [ ] Commit

### Task 5: 50-call soak
- [ ] Dial ~50 real calls over ~1 week; grep for `KERNEL_SHADOW DIVERGENCE`
- [ ] Weekly divergence-report run + investigation
- [ ] For each divergence category: decide "kernel is right, legacy is wrong" (needs fixing pre-2b) OR "kernel is wrong, needs its own fix"
- [ ] Ship fixes; re-soak
- [ ] Exit criterion: <5% of turns produce a divergence AND every remaining divergence has a written explanation

### Task 6: Ratification writeup
- [ ] Create `docs/rnd-2026-08/62-phase2-2a-ratification.md`
- [ ] Summarize: divergence rates, categories, fixes applied, remaining known-safe divergences
- [ ] Doc #58 update: mark 2a done, 2b unblocked
- [ ] Commit

## Success criteria
- 50+ real calls with shadow ON, divergence rate <5% per turn
- Divergence-report script useful for regressions later
- Kernel's `observed_tool_call` produces meaningful `kernel_would_dispatch` values (not `-`)
- Written ratification doc explains why 2b is safe to attempt

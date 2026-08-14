# PHASE2 sub-phase 2d — Retire Parallel Brain Paths

**Goal:** delete the legacy prompt-driven brain path for lanes the kernel now owns. Reduce the codebase's three-parallel-brains problem to one.

**Position:** cleanup. 1-2 weeks. Blocks on 2b + 2c ratified.

## Prereqs (blocking)
- 2b Knowledge ratified, flag has been ON for one tenant for ≥2 weeks with zero fallbacks-to-legacy triggered
- 2c Workflow ratified, same soak requirement
- No open bugs against kernel-driven lanes for ≥1 week

## Global Constraints
- **Nothing that soaked well disappears.** If the kernel path proved itself, the legacy path is dead weight. If a lane has ANY known issue with the kernel path, do NOT retire the legacy fallback for that lane.
- **Reactive brain + streaming brain stay** — they're orthogonal to lane routing; retiring them is a separate concern.
- **Feature flags remain in code for one release.** `dialogue_kernel_*_enabled` stays as a kill switch, defaulted to True. Removing the flag entirely happens in the NEXT release only if no rollback fired.

## Tasks

### Task 1: Audit surviving legacy call sites
- [ ] Grep for all `if kernel.is_enabled():` conditionals in brain.py
- [ ] Grep for all `lookup_faq` calls, `_reply_lies_about_booking` calls, `_reply_promises_wait_without_tool` calls
- [ ] For each: is the kernel path handling it now? Delete or keep as fallback?
- [ ] Write `docs/rnd-2026-08/65-retirement-audit.md` with the disposition per call site
- [ ] Commit — no code changes yet, just the audit

### Task 2: Retire legacy knowledge path
- [ ] Delete `lookup_faq` tool from tool registry for tenants with kernel_knowledge_enabled=True (per-tenant; other tenants keep the tool)
- [ ] Delete `shape_for_voice` remnants if any survived 2b
- [ ] Delete legacy RAG dispatcher path in `ComposeHandler`
- [ ] Run existing test suite; fix cascading test failures
- [ ] Commit

### Task 3: Retire legacy workflow path
- [ ] Delete inline booking-elicitation prompt sections
- [ ] Delete `_reply_lies_about_booking` (redundant now — WorkflowController + SpeechCommitGate cover it)
- [ ] Delete `_reply_promises_wait_without_tool` (SpeechCommitGate covers it)
- [ ] Keep SpeechCommitGate — it's ratified in doc #56 Addendum B.1
- [ ] Keep one-gen-one-commit lock — ratified in Addendum B.2
- [ ] Keep phone precondition in tool handler — ratified as safety net in Addendum B.3
- [ ] Run tests; fix
- [ ] Commit

### Task 4: 30-call regression soak
- [ ] Dial 30 real calls across all 8 SOAK scenarios
- [ ] Verify no legacy-path-only markers appear in per-call logs (`FAKE_WAIT_BLOCKED` etc. shouldn't fire because we deleted them)
- [ ] Verify kernel path handles everything
- [ ] Fix + re-soak if any scenario regresses

### Task 5: Doc updates + declare PHASE2 done
- [ ] `docs/rnd-2026-08/66-phase2-close.md` — PHASE2 complete
- [ ] Doc #58: PHASE2 done, PHASE3 unblocked
- [ ] Doc #56: Addendum F noting parallel-path retirement
- [ ] Commit

## Success criteria
- Codebase has ONE brain path per lane (grep should not find dual-path branches)
- 30-call soak zero critical failures
- ~1000 fewer lines of code (that was doc #56's implicit promise: "3 parallel brains" → 1)
- Feature flags remain as kill switches; default True

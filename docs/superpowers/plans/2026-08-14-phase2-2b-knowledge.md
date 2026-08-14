# PHASE2 sub-phase 2b — Knowledge Lane through Kernel

**Goal:** the kernel drives the "answer a question from the business KB" flow. Legacy `lookup_faq` tool + RAG dispatcher stay in place as fallback while the kernel path proves itself.

**Position:** doc #56 line 60 — smallest surface area, easiest to validate. Best first-lane-live because factual answers have objective right/wrong.

## Prereqs (blocking)
- 2a ratified (<5% divergence, `62-phase2-2a-ratification.md` written)
- RAG index exists at `apps/api/data/rag/kb.db` (already true)

## Global Constraints
- **Legacy path stays available** — feature flag `dialogue_kernel_knowledge_enabled` gates the kernel-driven path per-tenant. Default off.
- **When kernel path is on but returns nothing, fall back to legacy.** No "kernel said no answer" without asking the RAG-tool path too.
- **`shape_for_voice()` MUST die in this phase.** Doc #56 line 44: it's an unnecessary second LLM hop. Realizer renders facts directly.
- **Answers cite their source.** KB entry ID + confidence in the log. If confidence < `rag_confidence_threshold` (currently 0.7), do NOT speak — escalate or ask a clarifying question.

## Tasks

### Task 1: Structured facts layer
- [ ] Design `packages/rag/structured_facts.py`: typed objects for hours, address, services, staff, prices, insurance (not free-text)
- [ ] Migration: parse existing `business.json` into structured facts on load
- [ ] Test: 10 canned business profiles → structured facts extracted correctly
- [ ] Commit

### Task 2: KernelKnowledgeLane
- [ ] Create `packages/dialogue/knowledge_lane.py`: given a caller turn tagged as "question", produce a KernelAnswer (facts + citations + confidence)
- [ ] Wire kernel's `on_user_turn` → intent-classifier → knowledge-lane router
- [ ] Test: 20 caller turns × expected KernelAnswer
- [ ] Commit

### Task 3: Kill `shape_for_voice()`
- [ ] Trace every call site of `shape_for_voice()`; replace each with direct realizer templates
- [ ] Test: same 20 caller turns → same-quality spoken answer, one less LLM hop
- [ ] Delete `shape_for_voice()` function
- [ ] Commit

### Task 4: Wire in actor with feature flag
- [ ] `dialogue_kernel_knowledge_enabled` in settings
- [ ] Actor branch: if flag on AND question intent → KernelKnowledgeLane, else legacy
- [ ] Fallback: kernel returns None or low-confidence → legacy path
- [ ] Test with mocked kernel + mocked legacy → correct fallback logic
- [ ] Commit

### Task 5: 30-call soak with flag ON for one tenant
- [ ] Pick "test-clinic" tenant, flip flag on
- [ ] Dial 30 questions (mix: hours, insurance, staff, prices, off-topic)
- [ ] Score: correct facts, correct citations, latency vs 2a baseline
- [ ] Ship fixes; re-soak
- [ ] Exit: kernel path answers ≥95% correctly, latency within 10% of legacy

### Task 6: Ratification + doc updates
- [ ] `docs/rnd-2026-08/63-phase2-2b-ratification.md`
- [ ] Doc #58: mark 2b done
- [ ] Commit — 2c unblocked

## Success criteria
- Kernel-driven knowledge lane serves ≥95% of test questions correctly with citations
- `shape_for_voice()` deleted from codebase (grep returns zero hits outside its own definition, which is also gone)
- One less LLM hop per knowledge turn (verified in latency metrics)

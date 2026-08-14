# PHASE2 sub-phase 2c — Workflow Lane + Workflow-Controller Slot Capture

**Goal:** the kernel drives booking / reschedule / cancel flows. This is where R3 slim v1's deferred workflow-controller-driven phone capture (doc #56 Addendum C) finally lands.

**Position:** the riskiest sub-phase. Booking is where hallucinations turn into DB pollution. Doc #56 line 61: 2-3 weeks estimate. Assume 4-5.

## Prereqs (blocking)
- 2b ratified (Knowledge lane proven)
- R3 slim v1 shipped (already done — commit `5816793`)
- SpeechCommitGate + one-gen-one-commit lock live (already done — `817df97`, `25cba53`)
- Real calendar integration (Phase 4a) — if not ready, use FakeCalendar and mark this as "demo-only" pass

## Global Constraints
- **Workflow controller owns slot capture, NOT tools.** Tools become pure operations over already-validated inputs. Doc #56 Addendum C.
- **Phone precondition (R3 phase 4 slim v1) STAYS as safety net.** Workflow pre-validates; tool re-validates; failure at either layer prevents the write.
- **`assistant_response_id` + `utterance_revision_id`** (currently stubbed in `_response_revision_counter`) become authoritative for kernel-driven revisions crossing the CommitGate. See doc #56 Addendum B.2.
- **Rollback per lane.** If cancel/reschedule flows regress, revert JUST those; keep booking on kernel.
- **`dialogue_kernel_workflow_enabled` flag per-tenant.** Same shape as 2b.

## Tasks

### Task 1: WorkflowController scaffolding
- [ ] Create `packages/dialogue/workflow_controller.py`
- [ ] API: `start_booking(state)`, `handle_turn(text, state) -> WorkflowStep`, `commit(state) -> ToolResult`
- [ ] WorkflowStep: `PromptFor(slot) | Confirm(value) | Reject(reason) | Ready(fields)`
- [ ] State machine table: slots [caller_name, phone, service, start_iso, notes]; each has states [empty, in_capture, possible_needs_confirm, valid]; transitions on caller turns
- [ ] Test: 30 canned state × input → expected WorkflowStep transitions
- [ ] Commit

### Task 2: Wire slot capture into WorkflowController
- [ ] `WorkflowController.handle_turn` calls `actor.enter_slot_capture(kind, config, on_commit, on_confirm_needed, on_stall)` when a slot needs a value
- [ ] Actor's slot-capture layer (already exists) drives the STT/DTMF/ANI feed
- [ ] Committed value → back into WorkflowController state → next WorkflowStep
- [ ] Test: full booking flow start → PromptFor(name) → capture → PromptFor(phone) → capture with DTMF mid-flow → confirm → Ready
- [ ] Commit

### Task 3: Tool contracts become pure
- [ ] Modify `book_appointment`, `reschedule_appointment`, `cancel_appointment` handlers to REJECT unvalidated phone (Layer B error path exists; harden the messages so LLM never sees "close enough")
- [ ] Update prompt (`packages/core_agent/prompt.py`) to remove any "ask the caller for X" wording — LLM's job in this phase is intent + confirmation, NOT elicitation
- [ ] Test: LLM tries to book without a workflow-committed phone → tool returns `phone_missing` structured error → LLM re-asks
- [ ] Commit

### Task 4: `assistant_response_id` / `utterance_revision_id` — real semantics
- [ ] Extend `_response_revision_counter` from a counter to a mapping `{gen: [{revision_id, source, committed_at, superseded_by}]}`
- [ ] CommitGate consults this before releasing: only the latest un-superseded revision speaks
- [ ] Kernel emits new revisions via `WorkflowController.revise(gen, new_step)` — old revision marked `superseded_by=new_revision_id`
- [ ] Test: 3 canned scenarios where the kernel changes its mind mid-turn; verify only the final decision reaches TTS
- [ ] Commit

### Task 5: Feature flag + fallback path
- [ ] `dialogue_kernel_workflow_enabled` per tenant
- [ ] Actor branch: booking intent + flag on → WorkflowController; else legacy tool-loop
- [ ] Test: flag off → legacy; flag on + kernel crash → legacy; flag on + kernel returns None → legacy
- [ ] Commit

### Task 6: Booking flow soak (30 real calls)
- [ ] Karachi tester + Abdullah + Hamzah dial ~30 booking attempts on test-clinic (flag on)
- [ ] Track: bookings completed / dropped due to slot fail / dropped due to workflow bug
- [ ] Target: ≥90% success on well-formed calls; 100% no fake bookings
- [ ] Fix + re-soak

### Task 7: Reschedule + cancel flows
- [ ] Same pattern as booking: WorkflowController states, tool becomes pure, feature flag
- [ ] 15 calls each
- [ ] Commit

### Task 8: Ratification + doc updates
- [ ] `docs/rnd-2026-08/64-phase2-2c-ratification.md`
- [ ] Doc #58: mark 2c done, 2d unblocked
- [ ] Update R3 slim v1 addendum in doc #56: workflow-controller version now shipped

## Success criteria
- Workflow controller drives booking / reschedule / cancel with feature flag ON
- ≥90% booking success on well-formed real calls
- 0 fake bookings across 60+ soak calls
- Tools never elicit conversationally (grep prompt.py for "ask the caller" — should be 0)
- `assistant_response_id` / `utterance_revision_id` used for at least one revision case in soak logs

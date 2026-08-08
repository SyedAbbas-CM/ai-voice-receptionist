"""Propose → Confirm → Commit action protocol (Sprint 10 Track B).

The audit's diagnosis: the write guard is compensating for an unsafe
architecture — probabilistic validation around probabilistic proposal.
Fix is not another LLM, it's a 4-phase protocol that gives every
write action a real transaction boundary.

Phase 1 — PROPOSAL
    Build an ActionProposal with:
      * action_id (deterministic hash of task+kind+arguments)
      * evidence_ids per argument (which SlotEvidence justifies each)
      * idempotency_key (guarantees a retry doesn't duplicate)
    A proposal is safe to construct; nothing external happens yet.

Phase 2 — CONFIRMATION
    Match caller acknowledgment to a specific proposal.  Not just
    "caller said yes" — CallerConfirmation records which turn, which
    scope (fields), which confidence.

Phase 3 — COMMIT
    Execute the external write.  Coordinator holds a per-action
    lock keyed by idempotency_key so concurrent commits collapse to
    one.  Result is a CommitOutcome carrying external_id +
    committed_values.

Phase 4 — VERIFICATION
    The spoken confirmation is templated FROM committed_values, not
    from the proposal.  If a provider ID doesn't come back, we don't
    say "booked" — we say "let me check on that and call you back."

Kept as data types + coordinator here.  Provider-specific commit
adapters (`FakeCalendar`, `GoogleCalendar`, etc.) live under
packages/integrations/ — they implement `CommitAdapter` protocol.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Optional, Protocol

from pydantic import BaseModel, Field, model_validator

from .state import DialogueState, SlotEvidence, SlotStatus, TaskState

log = logging.getLogger(__name__)


class ActionKind(str, Enum):
    """Deterministic write actions.  Read actions do not go through
    this pipeline (no external write, no idempotency needed)."""
    BOOK_APPOINTMENT = "book_appointment"
    CANCEL_APPOINTMENT = "cancel_appointment"
    RESCHEDULE_APPOINTMENT = "reschedule_appointment"
    RECORD_DISPOSITION = "record_disposition"
    CAPTURE_DEPOSIT = "capture_deposit"


class ActionArgument(BaseModel):
    """One argument to a write action, with the evidence backing it.

    The commit coordinator refuses to execute a proposal if any
    required argument is missing evidence or if the referenced
    evidence has been superseded/rejected since proposal time."""
    name: str
    value: Any
    evidence_turn_id: str
    """The source_turn_id of the SlotEvidence that justifies this
    argument.  Coordinator re-validates that this evidence still
    exists and is active at commit time."""

    @classmethod
    def from_evidence(cls, name: str, evidence: SlotEvidence) -> "ActionArgument":
        return cls(
            name=name, value=evidence.value,
            evidence_turn_id=evidence.source_turn_id,
        )


class ActionProposal(BaseModel):
    """Phase 1 output.  Data-only, no side effects yet.

    action_id is deterministic: hash(task_id + kind + sorted args).
    Two identical proposals from the same call produce the same
    action_id — the coordinator's idempotency table sees them as one
    request.  Combined with the idempotency_key (which incorporates
    tenant + business), this survives:
      * Retry within the same call (network blip during commit)
      * Retry across calls (caller hangs up + re-dials before the
        commit result was persisted)"""
    action_id: str
    kind: ActionKind
    task_id: str
    tenant_id: str
    business_id: str
    arguments: list[ActionArgument]
    idempotency_key: str

    @classmethod
    def build(
        cls,
        kind: ActionKind,
        task_id: str,
        tenant_id: str,
        business_id: str,
        arguments: list[ActionArgument],
    ) -> "ActionProposal":
        """Deterministic construction — same inputs always yield the
        same action_id + idempotency_key."""
        arg_repr = sorted(
            [(a.name, _canonical(a.value)) for a in arguments],
            key=lambda x: x[0],
        )
        payload = json.dumps({
            "kind": kind.value,
            "task_id": task_id,
            "tenant_id": tenant_id,
            "business_id": business_id,
            "args": arg_repr,
        }, sort_keys=True)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        return cls(
            action_id=f"act_{digest}",
            kind=kind,
            task_id=task_id,
            tenant_id=tenant_id,
            business_id=business_id,
            arguments=arguments,
            idempotency_key=f"{tenant_id}:{business_id}:{kind.value}:{digest}",
        )


class CallerConfirmation(BaseModel):
    """Phase 2 output.  Records EXACTLY what the caller acknowledged.

    scope = which fields the caller's acknowledgment covered.
    Example: caller says "yes 10am works" — scope = ["start_iso"].
    Then agent asks "and your name is Sarah?" — caller says "yes" —
    that's a SECOND confirmation with scope = ["caller_name"].
    Coordinator requires the confirmation set to cover all fields
    before commit is allowed."""
    action_id: str
    caller_turn_id: str
    scope: list[str]
    confidence: float = Field(ge=0.0, le=1.0)
    source_text: str = ""

    @model_validator(mode="after")
    def _scope_non_empty(self) -> "CallerConfirmation":
        if not self.scope:
            raise ValueError("confirmation scope cannot be empty")
        return self


class CommitOutcome(str, Enum):
    """What happened at the external write."""
    SUCCESS = "success"
    DUPLICATE = "duplicate"        # idempotency collision → reuse prior
    CONFLICT = "conflict"          # slot no longer available
    PROVIDER_ERROR = "provider_error"
    TIMEOUT = "timeout"
    REJECTED = "rejected"          # policy refused (e.g., unconfirmed args)


@dataclass(frozen=True)
class CommitResult:
    """Phase 3 + 4 output.

    committed_values is the source of truth for the spoken
    confirmation — if the provider returned a normalized time or a
    truncated name, the caller hears what was actually saved."""
    outcome: CommitOutcome
    action_id: str
    external_id: Optional[str] = None
    committed_values: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


class CommitAdapter(Protocol):
    """Provider adapter.  Concrete calendars/CRMs implement this.

    Contract:
      * Must be idempotent w.r.t. idempotency_key.  Two calls with
        the same key return the same result, no duplicate side-effect.
      * MUST NOT raise on business-level failures (slot conflict,
        policy reject).  Return CommitResult with the appropriate
        outcome.  Exceptions are for infrastructure faults only."""

    async def commit(self, proposal: ActionProposal) -> CommitResult: ...


class CommitPolicyError(Exception):
    """Raised by coordinator when a proposal fails pre-commit checks."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class CommitCoordinator:
    """Orchestrates the 4-phase protocol.

    Per-call instance (constructed by session_manager or the actor
    session).  Holds an idempotency cache keyed by proposal action_id
    so repeated calls collapse to one external write."""

    def __init__(self, adapters: dict[ActionKind, CommitAdapter]) -> None:
        self._adapters = adapters
        # In-memory idempotency cache — per-actor lifetime.  Sprint 11
        # replaces with Redis-backed shared cache for multi-worker.
        self._results: dict[str, CommitResult] = {}
        # Per-action-id lock so concurrent commit() calls with the
        # same action_id wait for the first to complete.
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    # ── phase 1: propose ────────────────────────────────────────────

    def propose(
        self,
        kind: ActionKind,
        task: TaskState,
        state: DialogueState,
        argument_map: dict[str, str],
    ) -> ActionProposal:
        """Build a proposal from a task's active evidence.

        argument_map = {action_arg_name: slot_name_in_task}.
        Fails CLOSED if any slot has no active evidence or the
        evidence is at an insufficient status level."""
        arguments: list[ActionArgument] = []
        missing: list[str] = []
        for arg_name, slot_name in argument_map.items():
            ev = task.active_evidence(slot_name)
            if ev is None:
                missing.append(slot_name)
                continue
            if ev.status not in (SlotStatus.EXPLICIT, SlotStatus.CONFIRMED):
                raise CommitPolicyError(
                    "argument_status_insufficient",
                    f"{arg_name}={slot_name}: {ev.status.value}",
                )
            arguments.append(ActionArgument.from_evidence(arg_name, ev))
        if missing:
            raise CommitPolicyError("missing_evidence", ",".join(missing))

        return ActionProposal.build(
            kind=kind,
            task_id=task.task_id,
            tenant_id=state.tenant_id,
            business_id=state.business_id,
            arguments=arguments,
        )

    # ── phase 2: confirmation aggregation ───────────────────────────

    def check_confirmation_covers_all_arguments(
        self,
        proposal: ActionProposal,
        confirmations: list[CallerConfirmation],
    ) -> tuple[bool, list[str]]:
        """Returns (all_covered, missing_scope).

        Scope union of all confirmations for this action_id must
        cover every argument name in the proposal.  This is what
        stops "caller said yes to the time" from committing an
        unconfirmed phone number."""
        covered: set[str] = set()
        for c in confirmations:
            if c.action_id == proposal.action_id:
                covered.update(c.scope)
        arg_names = {a.name for a in proposal.arguments}
        missing = sorted(arg_names - covered)
        return len(missing) == 0, missing

    # ── phase 3+4: commit + verify ──────────────────────────────────

    async def commit(
        self,
        proposal: ActionProposal,
        confirmations: list[CallerConfirmation],
        task: TaskState,
        *,
        require_full_confirmation: bool = True,
    ) -> CommitResult:
        """Execute the write.  Fails CLOSED on any policy violation.

        * Verifies evidence for every argument is still ACTIVE (not
          superseded or rejected since proposal was built).
        * Verifies confirmation scope covers all arguments (unless
          require_full_confirmation=False for low-stakes actions).
        * Deduplicates via the coordinator's cache — same action_id
          returns the prior result.
        * Serializes concurrent commits via per-action lock."""
        # Fast-path: previously committed
        if proposal.action_id in self._results:
            prior = self._results[proposal.action_id]
            log.info(
                "commit dedup hit action_id=%s outcome=%s",
                proposal.action_id, prior.outcome.value,
            )
            return prior

        # Re-validate evidence hasn't been superseded since proposal built
        for arg in proposal.arguments:
            entries = task.slots.get(arg.name, [])
            match = next(
                (e for e in entries if e.source_turn_id == arg.evidence_turn_id),
                None,
            )
            if match is None:
                return self._reject(
                    proposal, "evidence_missing",
                    f"arg={arg.name} turn={arg.evidence_turn_id}",
                )
            if match.status in (SlotStatus.SUPERSEDED, SlotStatus.REJECTED):
                return self._reject(
                    proposal, "evidence_invalidated",
                    f"arg={arg.name} status={match.status.value}",
                )

        # Confirmation scope check
        if require_full_confirmation:
            covered, missing = self.check_confirmation_covers_all_arguments(
                proposal, confirmations,
            )
            if not covered:
                return self._reject(
                    proposal, "confirmation_scope_incomplete",
                    f"missing={missing}",
                )

        # Adapter dispatch — held under per-action lock so concurrent
        # commits with the same action_id serialize.
        lock = await self._get_lock(proposal.action_id)
        async with lock:
            # Re-check dedup inside the lock (someone else may have
            # completed while we waited)
            if proposal.action_id in self._results:
                return self._results[proposal.action_id]

            adapter = self._adapters.get(proposal.kind)
            if adapter is None:
                return self._reject(
                    proposal, "no_adapter", f"kind={proposal.kind.value}",
                )

            try:
                result = await adapter.commit(proposal)
            except Exception as e:
                log.exception("adapter raised on commit action=%s", proposal.action_id)
                result = CommitResult(
                    outcome=CommitOutcome.PROVIDER_ERROR,
                    action_id=proposal.action_id,
                    error=f"{type(e).__name__}: {e}",
                )

            self._results[proposal.action_id] = result
            return result

    # ── helpers ─────────────────────────────────────────────────────

    def _reject(self, proposal: ActionProposal, reason: str, detail: str = "") -> CommitResult:
        result = CommitResult(
            outcome=CommitOutcome.REJECTED,
            action_id=proposal.action_id,
            error=f"{reason}: {detail}" if detail else reason,
        )
        self._results[proposal.action_id] = result
        return result

    async def _get_lock(self, action_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._locks.get(action_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[action_id] = lock
        return lock

    def prior_result(self, action_id: str) -> Optional[CommitResult]:
        """For debugging + verification.  Returns the cached commit
        result for an action_id, or None if not yet committed."""
        return self._results.get(action_id)


# ── canonical value repr for deterministic hashing ─────────────────

def _canonical(value: Any) -> Any:
    """Normalize values for stable hashing across Python versions +
    JSON round-trips.  Handles datetime/UUID via str() coercion."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {k: _canonical(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    return str(value)

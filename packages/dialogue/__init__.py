"""Conversation State Kernel — Sprint 10 Track A.

Built in response to the 2026-08-04 intelligence audit's central
finding: the receptionist brain reasons over a transcript-as-memory
model that can't represent corrections, evidence provenance, or
plan/commit boundaries.

Design principles:
  * Deterministic reducer.  LLM PROPOSES state patches; Python code
    validates and applies.  Never let the model directly mutate.
  * Evidence-first slots.  Every slot value carries source_turn_id,
    source_role (caller/tool/business_profile), confidence, status
    (proposed/inferred/explicit/confirmed/superseded/rejected).
  * Task graph, not single intent.  Multi-part callers ("book + FAQ +
    claim status") become an agenda of TaskState.
  * Runs ALONGSIDE the existing CallState, not as a replacement.
    Migration path: brain writes state patches; downstream consumers
    (tool arg validation, playback ledger, delivery planner) migrate
    to reading from the kernel one at a time.

Public API:

    from packages.dialogue import (
        # Types
        SlotEvidence, SlotStatus, TaskKind, TaskStatus, TaskState,
        ConversationAgenda, DialogueState, StatePatch,

        # Reducer
        Reducer, reduce_patch, apply_correction,

        # Semantic plan (Track A2 — see plan.py)
        SemanticPlan, PlannedFact, PlannedQuestion,
    )
"""
from .state import (
    SlotEvidence,
    SlotStatus,
    SourceRole,
    TaskKind,
    TaskStatus,
    TaskState,
    ConversationAgenda,
    DialogueState,
)
from .reducer import (
    StatePatch,
    Reducer,
    reduce_patch,
    apply_correction,
    PatchRejected,
)
from .plan import (
    SemanticPlan,
    PlannedFact,
    PlannedQuestion,
    PlanOperation,
    DeliveryIntent,
)
from .commit import (
    ActionKind,
    ActionArgument,
    ActionProposal,
    CallerConfirmation,
    CommitOutcome,
    CommitResult,
    CommitAdapter,
    CommitCoordinator,
    CommitPolicyError,
)
from .temporal import (
    TemporalContext,
    TemporalResolver,
    ResolvedRange,
    Resolution,
    ImpossibleReason,
)
from .acoustic import (
    AcousticTurnFeatures,
    extract_features,
    energy_from_mulaw,
    count_pauses,
    speech_rate_wpm,
    repeated_phrase_count,
)
from .llm_capabilities import (
    LatencyClass,
    Operation,
    ModelCapabilities,
    CAPABILITY_TABLE,
    models_for_operation,
    preferred_order_for,
    preferred_model_for,
    capability_snapshot,
)

__all__ = [
    "SlotEvidence",
    "SlotStatus",
    "SourceRole",
    "TaskKind",
    "TaskStatus",
    "TaskState",
    "ConversationAgenda",
    "DialogueState",
    "StatePatch",
    "Reducer",
    "reduce_patch",
    "apply_correction",
    "PatchRejected",
    "SemanticPlan",
    "PlannedFact",
    "PlannedQuestion",
    "PlanOperation",
    "DeliveryIntent",
    "ActionKind",
    "ActionArgument",
    "ActionProposal",
    "CallerConfirmation",
    "CommitOutcome",
    "CommitResult",
    "CommitAdapter",
    "CommitCoordinator",
    "CommitPolicyError",
    "TemporalContext",
    "TemporalResolver",
    "ResolvedRange",
    "Resolution",
    "ImpossibleReason",
    "AcousticTurnFeatures",
    "extract_features",
    "energy_from_mulaw",
    "count_pauses",
    "speech_rate_wpm",
    "repeated_phrase_count",
    "LatencyClass",
    "Operation",
    "ModelCapabilities",
    "CAPABILITY_TABLE",
    "models_for_operation",
    "preferred_order_for",
    "preferred_model_for",
    "capability_snapshot",
]

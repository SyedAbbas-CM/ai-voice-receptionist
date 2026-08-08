"""Real-time call runtime primitives.

Sprint 7/8b: CallActor + CallEvent envelope + PlaybackLedger.

The three pieces together fix re-audit CRITICAL-08 (concurrent turns
racing inside a call) and deliver the temporal correctness foundation
the deep-research moat doc (`docs/rnd-2026-08/37-*.md`) called out as
the primary defensible asset.

Public API:

    from packages.runtime import (
        CallActor,          # one-per-call serialized state owner
        CallEvent,          # immutable event envelope with generation IDs
        EventSource,        # media/stt/llm/tool/tts/playback/timer/control
        CallState,          # actor state machine states
        PlaybackLedger,     # generated / queued / heard tracking
        CallActorRegistry,  # (call_id, tenant_id) -> CallActor lookup
    )
"""
from .call_event import CallEvent, EventSource
from .call_actor import CallActor, CallActorRegistry, CallState, get_registry
from .playback_ledger import PlaybackLedger, AudioChunk
from .streaming_stt_bridge import StreamingSTTBridge
from .turn_manager import (
    TurnManager, TurnEventKind, TurnManagerConfig, classify_short_utterance,
)
from .heard_text_reconciler import (
    split_into_sentences, split_into_playback_chunks,
    reconcile_transcript_on_interrupt,
)
from . import telemetry

__all__ = [
    "CallActor",
    "CallActorRegistry",
    "CallState",
    "CallEvent",
    "EventSource",
    "PlaybackLedger",
    "AudioChunk",
    "get_registry",
    "telemetry",
    "StreamingSTTBridge",
    "TurnManager",
    "TurnEventKind",
    "TurnManagerConfig",
    "classify_short_utterance",
    "split_into_sentences",
    "split_into_playback_chunks",
    "reconcile_transcript_on_interrupt",
]

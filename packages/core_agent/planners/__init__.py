"""Two-planner architecture for voice-agent turns (Sprint 9e).

The receptionist brain is split into two planners:

  * Semantic planner — WHAT to say.  Wraps the existing ReceptionistBrain
    and extracts a speech_act tag alongside the reply text.  This is
    the "primary reasoning" call: it decides the answer, tool_calls,
    and how to phrase things.

  * Performance planner — HOW to say it.  A fast, small LLM (Groq 8B)
    call that produces the VPL Delivery block: pace, pauses, style,
    intensity.  Runs in parallel with TTS.  Fails open to the
    deterministic default_delivery_for(speech_act) if the LLM errors
    or times out — the caller hears a well-delivered turn either way.

Design notes in `docs/superpowers/specs/2026-08-03-sprint9e-two-planner-design.md`.
"""
from .semantic import SemanticOutput, SemanticPlanner
from .performance import PerformancePlan, PerformancePlanner

__all__ = [
    "SemanticOutput",
    "SemanticPlanner",
    "PerformancePlan",
    "PerformancePlanner",
]

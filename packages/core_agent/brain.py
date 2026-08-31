from __future__ import annotations

import json
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from pydantic import BaseModel

from packages.schemas import (
    BusinessProfile,
    CallState,
    CallStatus,
    ToolCall,
    ToolDefinition,
    ToolResult,
    TranscriptTurn,
    TurnRole,
)

from .extractor import extract_fields
from .prompt import build_system_prompt
from .speech_sanitizer import sanitize_for_speech, enforce_agent_name

# 2026-08-24 (A1 wiring): hoisted to module scope per networking review.
# The intercept below reads `settings.next_action_policy_enabled` once
# per turn; keeping the import at module scope trims the per-turn import
# cost to zero.  If the import ever fails (should not, since brain.py
# already depends on other apps.api.app.* modules transitively), we
# fall back to a stub with the flag off — synth is disabled by default
# so this is behavior-preserving.
try:
    from apps.api.app.core.config import settings as _brain_settings
except Exception:  # pragma: no cover — defensive
    class _BrainSettingsStub:
        next_action_policy_enabled = False
    _brain_settings = _BrainSettingsStub()  # type: ignore[assignment]

if TYPE_CHECKING:
    from apps.api.app.providers.base import LLMProvider, LLMResponse
else:
    from apps.api.app.providers.base import LLMResponse


ToolHandler = Callable[[ToolCall], Awaitable[ToolResult]]


# 2026-08-11 (task #310): reply-side sanity check for fake booking
# confirmations.  We match on the LLM's WORDS (what the caller will
# actually hear), not the internal state.  If any of these phrases fire
# but no book_appointment tool call succeeded this turn, the reply is
# lying and we rewrite it.
import re as _re_book

_FAKE_BOOKING_PATTERNS = tuple(
    _re_book.compile(pat, _re_book.IGNORECASE)
    for pat in (
        r"\byou'?re all set\b",
        r"\byou'?re booked\b",
        r"\byou'?re confirmed\b",
        r"\bi'?ve booked\b",
        r"\bi'?ve got you (down|booked|scheduled) for\b",
        r"\b(?:appointment|booking) (?:is )?(?:booked|confirmed|locked in|set)\b",
        r"\blocked in\b",
        r"\bsee you\s+(?:then|on|at|next|this|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        r"\ball set for your\b",
    )
)

_BOOKING_TOOLS = frozenset({"book_appointment", "book_reservation", "book_viewing"})


# 2026-08-24 (A1 wiring): map booking-tool argument names → the flat
# key set the deterministic renderer expects.  Kept as a module-level
# constant so it's greppable and one-line to extend when a booking
# tool grows a new field or a new tool joins _BOOKING_TOOLS.
#
# Left side is what the LLM/tool schema uses; right side is what
# `next_action_synthesizer._render_confirm_action` reads.  If a
# booking tool uses different arg names in the future, extend here
# instead of teaching the renderer new keys — this keeps the renderer
# stable and the mapping close to the tool schema.
_BOOKING_ARG_TO_SLOT = {
    "caller_name": "caller_name",
    "name": "caller_name",
    "customer_name": "caller_name",
    "service": "service",
    "reason": "service",
    "appointment_type": "service",
    "date": "date",
    "start_date": "date",
    "start_iso": "start_iso",   # renderer knows to split ISO into date+time
    "time": "time",
    "start_time": "time",
    "phone": "phone",
    "phone_number": "phone",
}


def _extract_known_slots(state, tool_results_payload: list[dict]) -> dict[str, str]:
    """Build the flat `known_slots` dict the deterministic renderer wants.

    Reads from the successful booking tool receipt this turn.  Prefers
    the tool's own arguments (canonical values the tool executed on)
    over any transcript-scraped values (which may be pre-normalization
    or noisy).

    Falls back to empty on any issue — the renderer treats empty as
    "no facts, can't synthesize" and returns None → LLM fallback.
    """
    slots: dict[str, str] = {}
    try:
        for tr in tool_results_payload or []:
            if tr.get("name") not in _BOOKING_TOOLS:
                continue
            if tr.get("error") is not None:
                continue
            args = tr.get("arguments") or {}
            if not isinstance(args, dict):
                continue
            for arg_name, arg_val in args.items():
                slot_key = _BOOKING_ARG_TO_SLOT.get(arg_name)
                if not slot_key or not arg_val:
                    continue
                # start_iso needs splitting: "2026-08-26T14:30:00" → date+time
                if slot_key == "start_iso":
                    iso = str(arg_val)
                    if "T" in iso:
                        date_part, _, time_part = iso.partition("T")
                        slots.setdefault("date", date_part.strip())
                        # Trim seconds/timezone: "14:30:00Z" or "14:30:00-05:00"
                        t = time_part.split("+")[0].split("-")[0].split("Z")[0]
                        slots.setdefault("time", t.strip()[:5])
                    else:
                        # Bare date, no time — set date only
                        slots.setdefault("date", iso.strip())
                else:
                    slots.setdefault(slot_key, str(arg_val))
    except Exception:
        return slots
    return slots


# 2026-08-13 (R2 P0): fake-wait guard.
# If the LLM says "one moment / let me check / hold on / checking now"
# WITHOUT actually invoking a tool, the caller waits forever for a
# nothing that will never come.  This literally killed Hamzah's call
# (2026-08-13, log 19:06:21 "One moment, please." + LLM_STREAM_DONE
# tools=0 → 40 seconds of dead air until he asked "Are you still
# there?").  Guard rewrites the reply to a clarifying question so at
# least the caller stays engaged instead of getting ghosted.
_WAIT_LANGUAGE_PATTERNS = tuple(
    _re_book.compile(pat, _re_book.IGNORECASE)
    for pat in (
        r"\bone (?:moment|sec(?:ond)?|minute)\b",
        r"\blet me (?:check|look|see|find|verify)\b",
        r"\bi(?:'ll| will) (?:check|look|see|find|verify|pull up|grab)\b",
        r"\bhold on\b",
        r"\bhang on\b",
        r"\bgive me (?:a )?(?:sec(?:ond)?|moment|minute|second)\b",
        r"\bchecking (?:now|on that|availability|the calendar|for you)\b",
        r"\bjust a (?:sec(?:ond)?|moment|minute)\b",
        r"\blooking (?:that up|into (?:that|it))\b",
        r"\bbear with me\b",
    )
)


def _reply_promises_wait_without_tool(
    reply_text: str, tool_results: list[dict], tool_calls: list,
) -> bool:
    """R2 P0: True if the reply promises a wait/check but no tool call
    was made this round AND no tool result exists yet.

    tool_calls   = the LLM's tool_calls list from THIS response object
    tool_results = tool receipts accumulated across the whole turn's
                   loop (may include tools from earlier rounds)

    The check is intentionally strict on both fronts: the LLM is
    telling the caller to wait for something.  Either the LLM must
    actually invoke a tool in the same round OR we must have already
    completed a tool that would have produced the info they're waiting
    for.  Otherwise it's a lie and we rewrite."""
    if not reply_text:
        return False
    matched = False
    for pat in _WAIT_LANGUAGE_PATTERNS:
        if pat.search(reply_text):
            matched = True
            break
    if not matched:
        return False
    # LLM emitted tool_calls THIS round → not a fake wait, the tool
    # will actually run.
    if tool_calls:
        return False
    # A tool executed earlier in the same turn → also not a fake wait,
    # the wait phrase is just conversational filler over real work.
    if tool_results:
        return False
    return True  # wait language + zero tool activity anywhere = lie


def _reply_lies_about_booking(reply_text: str, tool_results: list[dict]) -> bool:
    """Return True if the reply text claims a booking was made but no
    successful booking-tool result exists in this turn's payload."""
    if not reply_text:
        return False
    matched_pat = False
    for pat in _FAKE_BOOKING_PATTERNS:
        if pat.search(reply_text):
            matched_pat = True
            break
    if not matched_pat:
        return False
    # Any booking tool succeeded this turn?  Success = present in the
    # payload AND error is None/falsy AND result isn't a blocked/error/
    # precondition-failure dict.
    for tr in tool_results:
        if tr.get("name") not in _BOOKING_TOOLS:
            continue
        if tr.get("error"):
            continue
        result = tr.get("result")
        if isinstance(result, dict):
            # R3 P4 slim v1: precondition failures return
            # phone_invalid / phone_missing / phone_partial /
            # phone_too_long / date_unparseable / date_ambiguous.
            # None of these are successful writes.
            if (
                result.get("blocked") or result.get("error")
                or result.get("phone_invalid") or result.get("phone_missing")
                or result.get("phone_partial") or result.get("phone_too_long")
                or result.get("date_unparseable") or result.get("date_ambiguous")
            ):
                continue
        return False  # a good booking receipt exists — reply is truthful
    return True  # reply claims booking but no successful tool call


class BrainTurnResult(BaseModel):
    reply: str
    state: CallState
    tool_results: list[dict] = []
    escalated: bool = False
    # Sprint 9e: speech_act tag for the two-planner path.  Defaults to
    # NEUTRAL so callers that don't opt into VPL get sensible fallback
    # delivery.  Only the semantic planner wrapper sets a non-default;
    # the raw brain currently returns NEUTRAL for everything until the
    # brain prompt is extended to emit it directly (planned in 9e).
    speech_act: str = "neutral"


class ReceptionistBrain:
    """Owns the conversation loop. One instance per call session.

    Loop per caller turn:
      1. Append caller text to transcript.
      2. Call LLM with system prompt + transcript + tools.
      3. If LLM returns tool_calls, dispatch each, append results, call LLM again.
      4. Stop when LLM returns plain text (or max_tool_iterations hit).
      5. Run a small extraction call to refresh structured fields.
    """

    # 2026-08-07: dropped 4 → 2 for voice latency.  Each iteration
    # is a full LLM roundtrip (~1-2s on Mistral, longer under load).
    # 4 iterations × 2 sec = 8s dead air where the caller thinks the
    # agent hung up.  Voice UX prioritizes speed over RAG depth: 2
    # iterations = one tool_call + one synthesis pass, still gets the
    # right answer for the vast majority of turns.
    MAX_TOOL_ITERATIONS = 2

    def __init__(
        self,
        llm: "LLMProvider",
        business: BusinessProfile,
        tools: list[ToolDefinition],
        tool_handler: ToolHandler,
        extractor_llm: Optional["LLMProvider"] = None,
        calendar=None,
    ) -> None:
        self.llm = llm
        self.business = business
        # T-SP1 (2026-08-19): register the `emit_semantic_plan` metadata
        # tool so the LLM can emit structured facts + pending_tasks
        # alongside its natural reply.  The tool is handled inline in
        # the tool loop below (never dispatched to `tool_handler`) — it
        # just captures the plan into `state._semantic_plan`.  See
        # `packages/core_agent/plan_realizer.py` for the schema.
        from .plan_realizer import semantic_plan_tool_definition
        self.tools = list(tools) + [semantic_plan_tool_definition()]
        self.tool_handler = tool_handler
        self.extractor_llm = extractor_llm or llm
        self.system_prompt = build_system_prompt(business)
        # Sprint 10 WIRING: optional handle so kernel_wiring can build
        # a CommitAdapter for booking.  Passed in from session_manager
        # (which owns the calendar cache).  None-safe: kernel_wiring
        # itself is gated by settings.dialogue_kernel_enabled.
        self.calendar = calendar
        # Lazy per-session KernelWiring — constructed on first turn so
        # tests can override before use.
        self._kernel = None

    async def greet(self, state: CallState) -> BrainTurnResult:
        # Compliance-safe greeting: AI-disclosure + optional recording consent.
        # State laws (CA, CO, TX and others in 2026) require these; even where
        # not legally mandated, prospective clients ask about it. Keep the
        # language natural — "I'm an AI assistant" reads as more human than
        # "Please note that this is an automated system."
        # Business owners can override via BusinessProfile.greeting_override
        # if they want a custom line.
        override = getattr(self.business, "greeting_override", None)
        if override:
            greeting = override
        else:
            # 2026-08-10: dropped disclosure + recording from DEFAULT.
            # Cached greeting was 15 sec of µ-law audio — callers were
            # waiting through 3 sentences of legal boilerplate before
            # they could speak.  Two tenants have already asked to
            # strip it because it makes the agent sound robotic.
            # Business owners who need it back set:
            #   business.ai_disclosure_enabled=True
            #   business.recording_notice_enabled=True
            # (defaults now False — opt-in, not opt-out).  For legal
            # coverage, restore per-business via profile flags.
            include_disclosure = getattr(self.business, "ai_disclosure_enabled", False)
            include_recording = getattr(self.business, "recording_notice_enabled", False)

            # 2026-08-10: tighter greeting — old one was 3 sentences +
            # disclosure = 7 sec of audio.  Now single sentence, ~2 sec.
            # 2026-08-25 (compliance sweep): reworded disclosure to
            # match the "never flat-say I'm an AI" identity rule.
            # "Automated receptionist" satisfies FCC/state automation-
            # disclosure requirements without breaking the role.
            # Recording notice covers two-party-consent states
            # (CA/FL/IL/MD/MA/MT/NV/NH/PA/WA) — plain-English so it
            # counts as "notice" under state wiretap statutes.
            # 2026-08-31 CALL-BUG-08: include agent's first name in the
            # greeting. "This is Ava at Smile Dental" sounds infinitely
            # more like a real receptionist than "Thanks for calling
            # Smile Dental Clinic, how can I help?" — and gives the
            # caller a name to remember + refer back to.
            _agent_name = getattr(self.business, "agent_name", None) or "Ava"
            parts = [
                f"Thanks for calling {self.business.name}, "
                f"this is {_agent_name} — how can I help?"
            ]
            if include_disclosure:
                parts.append("You're speaking with our automated receptionist.")
            if include_recording:
                parts.append("This call may be recorded for quality.")
            greeting = " ".join(parts)

        state.add_turn(TranscriptTurn(role=TurnRole.ASSISTANT, text=greeting))
        return BrainTurnResult(reply=greeting, state=state)

    def _ensure_kernel(self, state: CallState):
        """Sprint 10 WIRING: lazily build a KernelWiring for this call.
        Returns None if the kernel is disabled or wiring construction
        fails — brain works without it either way."""
        if self._kernel is not None:
            return self._kernel
        try:
            from .kernel_wiring import KernelWiring
            from packages.integrations.calendar_commit_adapter import (
                build_default_adapters,
            )
            adapters = build_default_adapters(
                self.calendar, business=self.business,
            ) if self.calendar is not None else {}
            self._kernel = KernelWiring(
                call_state=state,
                business_id=state.business_id,
                tenant_id=state.tenant_id,
                business_timezone=self.business.timezone,
                business_hours=self.business.hours,
                commit_adapters=adapters,
            )
        except Exception as e:
            import logging as _l
            _l.getLogger(__name__).warning("kernel wiring init failed: %s", e)
            self._kernel = None
        return self._kernel

    async def handle_user_turn(
        self, state: CallState, user_text: str,
        on_delta=None,
        on_tool_call=None,
        on_tool_receipt=None,
    ) -> BrainTurnResult:
        """on_delta: optional Callable[[str], Awaitable[None]] fired for
        each streamed token from the FINAL (no-tool-calls) LLM reply.
        The caller (twilio_actor) uses this to pipe tokens into TTS as
        sentence boundaries land. When on_delta is None or the resolved
        provider lacks stream_complete, we use the batch path.

        on_tool_call:   optional Callable[[str], Awaitable[None]] fired
                        once per tool the LLM dispatched — BEFORE the
                        tool handler runs.  Payload is the tool name.
                        Used by SpeechCommitGate to release held
                        WAIT_PROMISE sentences once a real tool is in
                        flight (the "one moment" is now honest).

        on_tool_receipt: optional Callable[[str, bool], Awaitable[None]]
                        fired once per tool AFTER the handler returns.
                        Payload is (tool_name, ok).  ok=True means the
                        tool succeeded (no error, not blocked).  Used
                        by the gate to release ACTION_CONFIRMATION
                        sentences ("you're booked") only after the
                        matching receipt is real.
        """
        state.add_turn(TranscriptTurn(role=TurnRole.USER, text=user_text))
        # 2026-08-21 NET: bump the extractor-throttle counter ONCE per
        # turn entry.  All seven `_refresh_extraction_bg` call sites
        # inside this turn will see the same stable value → modulo
        # check evaluates consistently → extractor fires once per
        # 3 turns as the docstring intends.
        state._extractor_turn_idx = getattr(state, "_extractor_turn_idx", -1) + 1  # type: ignore[attr-defined]

        # K3+K4 (2026-08-05): classify turn intent BEFORE brain fires so
        # the persona prompt has explicit branches for correction /
        # commitment / clarification / rejection / chitchat.  Regex-only,
        # ~5µs.  Stashed on state so the prompt builder can read it.
        try:
            from .classifiers.turn_intent import classify_turn_intent
            intent = classify_turn_intent(user_text)
            state.last_turn_intent = intent
            if intent.system_note:
                import logging as _l
                _l.getLogger(__name__).info(
                    "turn intent=%s conf=%.2f matched=%r",
                    intent.intent.value, intent.confidence, intent.matched[:40],
                )
        except Exception as e:
            import logging as _l
            _l.getLogger(__name__).warning("turn intent classify failed: %s", e)

        # Sprint 10 WIRING: kernel gets first look at the caller text.
        # Discovers tasks; downstream reducer + coordinator work off
        # the state it builds.  No-ops when settings.dialogue_kernel_enabled
        # is False.
        kernel = self._ensure_kernel(state)
        if kernel is not None and kernel.is_enabled():
            try:
                turn_id = f"turn_{len(state.transcript)}"
                kernel.on_user_turn(user_text, turn_id)
            except Exception as e:
                import logging as _l
                _l.getLogger(__name__).warning(
                    "kernel.on_user_turn failed: %s", e,
                )

        # EMERGENCY INTERCEPT — highest priority, runs BEFORE anything else.
        # Missing a real emergency is the top-cited liability failure in 2026
        # receptionist products. Fast regex check; never touches an LLM by
        # default. Full details: packages/core_agent/emergency_classifier.py
        # and docs/rnd-2026-07/05-nightmare-callers.md category 10.
        from .emergency_classifier import classify_emergency
        emergency = classify_emergency(user_text)
        if emergency.is_emergency:
            escalation_msg = emergency.escalation_message
            state.add_turn(TranscriptTurn(role=TurnRole.ASSISTANT, text=escalation_msg))
            state.status = CallStatus.ESCALATED
            state.escalation_reason = emergency.reason
            self._refresh_extraction_bg(state)
            return BrainTurnResult(
                reply=escalation_msg,
                state=state,
                escalated=True,
                tool_results=[{
                    "name": "emergency_escalation",
                    "arguments": {"category": emergency.category,
                                  "matched": emergency.matched_text},
                    "result": {"escalated": True, "reason": emergency.reason},
                    "error": None,
                }],
            )

        # Input guard: short-circuit obvious jailbreak/injection attempts
        # BEFORE we spend LLM tokens on them. The system prompt has an
        # identity-lock clause as backup; this is belt-and-suspenders.
        from .input_guard import is_probable_injection, classify_injection, safe_reply_for
        if is_probable_injection(user_text):
            kind = classify_injection(user_text)
            safe_reply = safe_reply_for(self.business.name, kind)
            state.add_turn(TranscriptTurn(role=TurnRole.ASSISTANT, text=safe_reply))
            self._refresh_extraction_bg(state)
            return BrainTurnResult(reply=safe_reply, state=state)

        tool_results_payload: list[dict] = []
        escalated = False

        from packages.observability import get_tracer
        tracer = get_tracer()

        # 2026-08-27 (task #120): NextActionPolicy per-turn injection.
        # ChatGPT audit 2026-08-26 H-P0.1: policy only governed the
        # post-booking synth branch, not general turns.  Now every
        # turn:
        #   1. Runs the TurnSignalReducer on the caller text
        #   2. Populates ConversationDecisionState with real signals
        #   3. Asks NextActionPolicy for the chosen action + ack
        #   4. Renders a system-note directive telling the LLM which
        #      action shape + which ack lane + how brief to be
        # LLM still verbalizes; policy decides shape.  Feature-flagged
        # by settings.next_action_policy_enabled (default False =
        # zero behavior change).
        _policy_directive: Optional[str] = None
        _policy_decision = None
        # Task #150 (context discovery) directive — set inside the
        # policy-enabled block below when the resolved service needs
        # context.  Declared here so downstream injection code can
        # reference it unconditionally.
        _discovery_directive: Optional[str] = None
        if getattr(_brain_settings, "next_action_policy_enabled", False):
            try:
                from .next_action_synthesizer import (
                    build_decision_state_with_signals,
                )
                from .policy_directive import render_policy_directive
                from packages.dialogue.next_action_policy import (
                    NextActionPolicy,
                )
                # Extract last agent utterance for reducer's structured-ask
                # heuristic ("did the agent just ask for phone?").
                _last_agent_text = ""
                for _t in reversed(state.transcript):
                    if _t.role == TurnRole.ASSISTANT and _t.text:
                        _last_agent_text = _t.text
                        break
                # 2026-08-29 (BUG #146 fix, from live test call
                # CA3dac680ae8661459bc74735603f2cbc9): compute the
                # `missing` slots BEFORE handing to NextActionPolicy.
                # Previous code left this defaulting to [] which made
                # policy always fall through to action=ANSWER — every
                # ASK_SLOT branch (and the entire LK slot-capture wire)
                # was dormant on live calls.  compute_missing_slots
                # detects booking-intent from recent caller utterances
                # and returns the ordered list of book_appointment
                # required slots not yet in known_slots.  Returns [] on
                # non-booking turns (ANSWER path preserved).
                from .missing_slots import (
                    compute_missing_slots,
                    recent_caller_texts_from_state,
                    resolve_service_from_utterances,
                )
                # 2026-08-31 (BUG #158 fix): state._collected_slots is
                # the persistent cross-turn slot dict.  Live diagnosis
                # from CAc66749590f6e53986eec4210e49bb425 showed known={}
                # and missing=[] on ALL 13 policy_decision events —
                # extractor ran per-turn but its output was thrown away
                # at function scope.  This dict outlives the function
                # call: seeded from state on entry, updated by all three
                # write sites (utterance resolver, discovery completion,
                # booking tool receipts), read back by _extract_known_slots.
                _persisted = dict(
                    getattr(state, "_collected_slots", None) or {}
                )
                _known_slots = _extract_known_slots(state, [])
                # Persisted values are the FLOOR — tool receipts may
                # overwrite (they're canonical), but per-turn recompute
                # never wipes prior turns' knowledge.
                for _k, _v in _persisted.items():
                    _known_slots.setdefault(_k, _v)
                _recent_caller_texts = (
                    recent_caller_texts_from_state(state)
                )
                # Include the current user_text — it's the freshest
                # signal + may not be in state.transcript yet depending
                # on where in the turn boundary we are.
                if user_text and user_text not in _recent_caller_texts:
                    _recent_caller_texts.insert(0, user_text)
                # 2026-08-30 (BUG #149 fix): augment known_slots with
                # service extracted from caller utterance.
                # _extract_known_slots reads only from booking tool
                # receipts, so 'an exam' / 'follow-up' / 'cleaning'
                # never landed there until book_appointment ran — and
                # book_appointment couldn't run because service was
                # 'missing.'  Break the deadlock: consult
                # resolve_service on recent utterances directly.
                # 2026-08-31 (CALL-BUG-06 fix B): also try to re-resolve
                # when the FRESH caller utterance (this turn only) yields
                # a stronger match than what's persisted. Guards against
                # a bad turn-1 fuzzy match sticking forever — real trace
                # CAbd671430f1297c1bbe0640a977060f1f had "with" fuzzy-
                # matching "New patient exam with X-rays" (0.60) on
                # turn 03, and every subsequent explicit "follow-up" was
                # ignored because service was already set. We only
                # overwrite if THIS turn's utterance gets an EXACT match.
                _fresh_utt = user_text if user_text else ""
                if not _known_slots.get("service"):
                    _biz = getattr(state, "business", None) or (
                        getattr(self, "business", None)
                    )
                    _spoken_service = resolve_service_from_utterances(
                        _recent_caller_texts, _biz,
                    )
                    if _spoken_service:
                        _known_slots["service"] = _spoken_service
                        # BUG #158 fix write site 1: persist for later turns.
                        try:
                            if not hasattr(state, "_collected_slots"):
                                state._collected_slots = {}
                            state._collected_slots["service"] = (
                                _spoken_service
                            )
                        except Exception:
                            pass
                        # Also emit ServiceResolutionEvent so /trace shows
                        # WHICH utterance mapped to WHICH canonical name
                        # (previously silent — networking couldn't bisect
                        # whether the extractor ran on the Christiaan call).
                        try:
                            from packages.observability.humanness_events import (
                                ServiceResolutionEvent as _SRE,
                                emit_humanness_event as _emit_sre,
                            )
                            _emit_sre(_SRE(
                                call_id=state.session_id or "?",
                                tenant_id=getattr(
                                    state, "tenant_id", "default",
                                ),
                                session_id=state.session_id or "?",
                                spoken=(user_text or "")[:200],
                                kind="match_from_utterance",
                                canonical_name=_spoken_service,
                            ))
                        except Exception:
                            pass
                elif _fresh_utt.strip():
                    # Service already set — but if THIS turn's fresh
                    # utterance produces an EXACT (not fuzzy) match to
                    # a DIFFERENT service, the prior value was probably
                    # a bad fuzzy stick (CALL-BUG-06). Overwrite. Never
                    # downgrade to fuzzy — only exact wins.
                    _biz = getattr(state, "business", None) or (
                        getattr(self, "business", None)
                    )
                    try:
                        from packages.integrations.service_aliases import (
                            resolve_service as _rs,
                            ServiceMatchKind as _SMK,
                        )
                        _svcs = getattr(_biz, "services", None) if _biz else None
                        if _svcs:
                            _fresh_match = _rs(_fresh_utt, _svcs)
                            if (
                                _fresh_match.kind == _SMK.MATCH_EXACT
                                and _fresh_match.canonical_name
                                and _fresh_match.canonical_name
                                    != _known_slots.get("service")
                            ):
                                _prior = _known_slots.get("service")
                                _known_slots["service"] = (
                                    _fresh_match.canonical_name
                                )
                                try:
                                    if not hasattr(
                                        state, "_collected_slots"
                                    ):
                                        state._collected_slots = {}
                                    state._collected_slots["service"] = (
                                        _fresh_match.canonical_name
                                    )
                                    # If the prior value seeded a
                                    # discovery orchestrator for a
                                    # different service, tear it down —
                                    # a new orchestrator (if any) will
                                    # open on the correct service below.
                                    if getattr(
                                        state, "_context_discovery", None,
                                    ) is not None:
                                        state._context_discovery = None
                                except Exception:
                                    pass
                                # Trace event: which utterance corrected
                                # which prior wrong service.
                                try:
                                    from packages.observability.humanness_events import (
                                        ServiceResolutionEvent as _SRE2,
                                        emit_humanness_event as _em2,
                                    )
                                    _em2(_SRE2(
                                        call_id=state.session_id or "?",
                                        tenant_id=getattr(
                                            state, "tenant_id", "default",
                                        ),
                                        session_id=state.session_id or "?",
                                        spoken=(_fresh_utt or "")[:200],
                                        kind="corrected_from_utterance",
                                        canonical_name=_fresh_match.canonical_name,
                                        reason=f"overwrote prior {_prior!r}",
                                    ))
                                except Exception:
                                    pass
                    except Exception:
                        pass
                # 2026-08-30 (task #150 wire): DISCOVER_CONTEXT branch.
                # When the resolved service needs context (e.g.
                # 'Follow-up visit' needs original_procedure /
                # original_provider / original_visit_date) AND we
                # don't yet have those context slots, the discovery
                # orchestrator takes over.  Policy is suppressed on
                # this turn — the LLM sees ONLY the discovery
                # directive (narrow scope, same LK sub-agent shape as
                # slot-capture prompts) and asks the next context
                # question.  Once all context slots are filled,
                # orchestrator hands back and normal ASK_SLOT
                # progresses.  See audit at
                # docs/product/journey-audit-follow-up-clinic-2026-08-29.md
                # for the gap this closes.
                _discovery_directive: Optional[str] = None
                try:
                    from packages.dialogue.context_discovery import (
                        ContextDiscoveryOrchestrator,
                    )
                    _svc = _known_slots.get("service")
                    _existing_disc = getattr(
                        state, "_context_discovery", None,
                    )
                    # Build one if service is known AND we don't
                    # already have an orchestrator for it.
                    if _svc and _existing_disc is None:
                        _new_disc = (
                            ContextDiscoveryOrchestrator.for_service(_svc)
                        )
                        if _new_disc is not None:
                            state._context_discovery = _new_disc
                            _existing_disc = _new_disc
                    # If orchestrator exists AND is not complete,
                    # inject its directive.
                    if (
                        _existing_disc is not None
                        and not _existing_disc.is_complete()
                    ):
                        _discovery_directive = (
                            _existing_disc.as_directive_note()
                        )
                    # If orchestrator is complete, tear down so it
                    # doesn't re-fire on future turns.  BEFORE
                    # teardown, stash the collected answers +
                    # notes-prefix on state so the booking-tool
                    # augmenter (task #144) can put them in
                    # book_appointment(notes=) for the front-desk.
                    if (
                        _existing_disc is not None
                        and _existing_disc.is_complete()
                    ):
                        try:
                            state._discovery_answers = (
                                _existing_disc.collected_answers()
                            )
                            state._discovery_notes_prefix = (
                                _existing_disc.as_notes_prefix()
                            )
                        except Exception:
                            pass
                        try:
                            state._context_discovery = None
                        except Exception:
                            pass
                except Exception as _disc_err:
                    import logging as _disc_log
                    _disc_log.getLogger(__name__).warning(
                        "context_discovery wire failed (non-fatal): %s",
                        _disc_err,
                    )
                _missing = compute_missing_slots(
                    known_slots=_known_slots,
                    recent_caller_texts=_recent_caller_texts,
                )
                _decision_state = build_decision_state_with_signals(
                    known_slots=_known_slots,
                    last_caller_text=user_text,
                    last_agent_text=_last_agent_text,
                    missing=_missing,
                )
                # 2026-08-30 (task #141): emit TurnSignalReducedEvent
                # so the /trace view shows the reducer's read of the
                # caller's utterance every turn.  Reasons list surfaces
                # WHY each signal fired — grep for 'reasons contains
                # hardship_kw:hurt' etc from a call log to bisect ACK
                # failures.  Defensive: humanness events never crash
                # the call path.
                try:
                    from packages.observability.humanness_events import (
                        TurnSignalReducedEvent as _TSRE,
                        emit_humanness_event as _emit_tsr,
                    )
                    _emit_tsr(_TSRE(
                        call_id=state.session_id or "?",
                        tenant_id=getattr(
                            state, "tenant_id", "default",
                        ),
                        session_id=state.session_id or "?",
                        last_caller_text=(user_text or "")[:400],
                        caller_shared_hardship=bool(getattr(
                            _decision_state,
                            "caller_shared_hardship", False,
                        )),
                        caller_corrected_us=bool(getattr(
                            _decision_state,
                            "caller_corrected_us", False,
                        )),
                        caller_is_dictating=bool(getattr(
                            _decision_state,
                            "caller_is_dictating", False,
                        )),
                        caller_asked_to_wait=bool(getattr(
                            _decision_state,
                            "caller_asked_to_wait", False,
                        )),
                        reasons=list(getattr(
                            _decision_state, "reasons", []
                        ) or []),
                    ))
                except Exception:
                    pass
                _policy_decision = NextActionPolicy().decide(_decision_state)
                _last_ack = getattr(state, "_last_ack", None)
                _policy_directive = render_policy_directive(
                    _policy_decision, last_ack=_last_ack,
                )
                # 2026-08-29 (task #97 second half): let observers act
                # on the decision.  Actor uses this to fire
                # enter_slot_capture(kind="phone") on ASK_SLOT
                # decisions where requested_slot=="phone", so the
                # NEXT caller turn feeds the structured slot session
                # instead of the general brain.  Callback is optional;
                # missing → business-as-usual.
                _on_dec = getattr(
                    state, "_on_policy_decision", None,
                )
                if _on_dec is not None and _policy_decision is not None:
                    try:
                        await _on_dec(_policy_decision)
                    except Exception as _cb_err:
                        import logging as _cb_log
                        _cb_log.getLogger(__name__).warning(
                            "policy-decision callback failed "
                            "(non-fatal): %s", _cb_err,
                        )
                # Stash last ack for next-turn recency guard.
                if _policy_decision is not None:
                    state._last_ack = _policy_decision.acknowledgment
                import logging as _pol_log
                _pol_log.getLogger(__name__).info(
                    "POLICY_DECISION action=%s ack=%s delivery=%s",
                    _policy_decision.action.value if _policy_decision else "?",
                    (_policy_decision.acknowledgment.value
                        if _policy_decision and _policy_decision.acknowledgment
                        else "?"),
                    (_policy_decision.delivery_intent.value
                        if _policy_decision else "?"),
                )
                # 2026-08-29 (humanness traceability): typed
                # PolicyDecisionEvent for the trace view.  Renders in
                # the humanness timeline as
                #   "Turn policy decided next action" — Action=X,
                #    ack=Y, delivery=Z, requesting slot: <slot>
                try:
                    from packages.observability.humanness_events import (
                        PolicyDecisionEvent as _PDE,
                        emit_humanness_event as _emit_pde,
                    )
                    if _policy_decision is not None:
                        _emit_pde(_PDE(
                            call_id=state.session_id or "?",
                            tenant_id=getattr(
                                state, "tenant_id", "default",
                            ),
                            session_id=state.session_id or "?",
                            action=_policy_decision.action.value,
                            acknowledgment=(
                                _policy_decision.acknowledgment.value
                                if _policy_decision.acknowledgment
                                else None
                            ),
                            delivery_intent=(
                                _policy_decision.delivery_intent.value
                            ),
                            max_tokens=getattr(
                                _policy_decision, "max_tokens", None,
                            ),
                            requested_slot=getattr(
                                _policy_decision,
                                "requested_slot", None,
                            ),
                            must_include_facts_count=len(
                                getattr(
                                    _policy_decision,
                                    "must_include_facts", []
                                ) or []
                            ),
                        ))
                except Exception:
                    pass
            except Exception as _pol_err:
                import logging as _pol_log
                _pol_log.getLogger(__name__).warning(
                    "policy directive rendering failed (falling back to "
                    "LLM improvise): %s", _pol_err,
                )
                _policy_directive = None

        # LK steal #7 (2026-08-29): if a slot-capture sub-agent prompt
        # is attached to the state, use it INSTEAD OF the wider system
        # prompt.  The whole point of LK's sub-agent pattern is that
        # the narrow scope knows nothing about the wider agent — it
        # only knows how to capture the one slot cleanly.  Mixing both
        # prompts defeats the purpose (small model gets confused by
        # the wider persona/tool surface).
        #
        # The actor stashes this on state as `_slot_capture_prompt`
        # for the duration of the capture.  Cleared when the actor
        # calls exit_slot_capture.  Non-slot turns are unaffected.
        # 2026-08-30 (Phase 2 wire, per networking's 3b99cbd):
        # stash opening_system_prompt on first turn so session_manager
        # can persist it to SessionRow.opening_system_prompt.  This is
        # the WIDER prompt (what the agent looks like without any
        # sub-agent scope).  Persist-side has upsert semantics that
        # protect against sub-agent scope overwriting this on later
        # persists — so it's safe to set every turn defensively.
        try:
            if not getattr(state, "_opening_system_prompt", None):
                state._opening_system_prompt = self.system_prompt
        except Exception:
            pass
        _slot_prompt = getattr(state, "_slot_capture_prompt", None)
        _slot_prompt_active = bool(
            _slot_prompt
            and getattr(_slot_prompt, "instructions", "")
        )
        # 2026-08-30 (Phase 2 wire, per networking's 3b99cbd): stash
        # the instructions delta on state so session_manager can
        # persist it on the TranscriptTurn.  Two cases: (a) slot-
        # capture scope IS active → the narrow prompt is the delta;
        # (b) discovery scope IS active → the discovery directive is
        # the delta; (c) neither → NULL delta (most turns).  LK
        # judges reconstruct effective instructions per turn by
        # walking backward to find the most recent non-null delta,
        # falling back to opening_system_prompt (also stashed by
        # session_manager on first LLM prep).
        try:
            if _slot_prompt_active:
                state._pending_instructions_delta = (
                    _slot_prompt.instructions
                )
            elif _discovery_directive:
                state._pending_instructions_delta = _discovery_directive
            else:
                # Detect exit-from-scope for the delta sentinel.  If
                # the previous turn had a delta and this turn doesn't,
                # write the sentinel so the judge knows scope resumed
                # to wider.  Read-back from state is fine — this is
                # ephemeral per-turn.
                _prev = getattr(state, "_last_delta_kind", None)
                if _prev in ("slot_capture", "discovery"):
                    state._pending_instructions_delta = "exit_scope"
                else:
                    state._pending_instructions_delta = None
        except Exception:
            pass
        # Track the delta kind so exit detection works next turn.
        try:
            if _slot_prompt_active:
                state._last_delta_kind = "slot_capture"
            elif _discovery_directive:
                state._last_delta_kind = "discovery"
            else:
                state._last_delta_kind = None
        except Exception:
            pass
        if _slot_prompt_active:
            # Log + emit for traceability.  Every slot-capture turn shows
            # up in /trace so business owners see the narrow-scope
            # branch fire (and can bisect if capture goes sideways).
            import logging as _slot_log
            _slot_log.getLogger(__name__).info(
                "SLOT_CAPTURE_PROMPT_ACTIVE session=%s tools_hint=%s",
                state.session_id,
                getattr(_slot_prompt, "tools_hint", ()),
            )
            try:
                from packages.observability.call_event_log import (
                    get_call_event_log, CallEvent as _CE_sp,
                    EventSourceKind as _SK_sp,
                )
                _elog = get_call_event_log()
                if _elog is not None:
                    _elog.write(_CE_sp(
                        call_id=state.session_id or "?",
                        tenant_id=getattr(state, "tenant_id", "default"),
                        source=_SK_sp.LLM,
                        kind="slot_capture_prompt_active",
                        payload={
                            "tools_hint": list(
                                getattr(_slot_prompt, "tools_hint", ())
                            ),
                            "instructions_chars": len(
                                getattr(_slot_prompt, "instructions", "")
                            ),
                        },
                    ))
            except Exception:
                pass
        for _ in range(self.MAX_TOOL_ITERATIONS):
            # 2026-08-30 (task #151): compute per-iteration effective
            # tool list.  When context discovery is active we splice
            # in `answer_context_task` (and `regress_context_tasks`
            # once at least one task has been visited) so the LLM has
            # the concrete mechanism to advance the orchestrator.
            # Regular tool set otherwise (self.tools).
            _effective_tools = self.tools
            try:
                _disc_for_tools = getattr(
                    state, "_context_discovery", None,
                )
                if (
                    _disc_for_tools is not None
                    and not _disc_for_tools.is_complete()
                ):
                    from packages.dialogue.context_discovery import (
                        build_discovery_tools,
                    )
                    _disc_tools = build_discovery_tools(_disc_for_tools)
                    if _disc_tools:
                        _effective_tools = (
                            list(self.tools) + list(_disc_tools)
                        )
            except Exception as _et_err:
                import logging as _et_log
                _et_log.getLogger(__name__).warning(
                    "discovery tools injection failed (falling back "
                    "to base tools): %s", _et_err,
                )
                _effective_tools = self.tools
            if _slot_prompt_active:
                # Narrow sub-agent scope.  No wider system prompt, no
                # turn intent, no policy directive.
                messages = [{
                    "role": "system",
                    "content": _slot_prompt.instructions,
                }]
            else:
                messages = [{"role": "system", "content": self.system_prompt}]
                # K3+K4: inject the turn-intent hint as a fresh system note
                # ONLY for this turn (not persisted to state.transcript so
                # it doesn't pollute future turns).
                intent = getattr(state, "last_turn_intent", None)
                if intent is not None and getattr(intent, "system_note", ""):
                    messages.append({"role": "system", "content": intent.system_note})
                # 2026-08-27 (task #120): inject the policy directive AFTER
                # turn-intent so the LLM sees the chosen action last (most
                # recent system message tends to bind tightest on gpt-4o-
                # mini class models).  Skipped when directive is None
                # (flag off OR renderer errored) — LLM improvises as before.
                if _policy_directive:
                    messages.append({
                        "role": "system", "content": _policy_directive,
                    })
                # 2026-08-30 (task #150): DISCOVER_CONTEXT branch.
                # When active, appended LAST so the LLM sees the
                # discovery directive as most recent + most binding.
                # This overrides normal policy asking — while
                # context-discovery is open, LLM asks the current
                # context question and NOTHING else.  Same shape as
                # LK's beta/workflows/task_group.py active-task
                # instructions.
                if _discovery_directive:
                    messages.append({
                        "role": "system",
                        "content": _discovery_directive,
                    })
            messages.extend(state.to_llm_messages())
            with tracer.span(
                "gen_ai.chat_completion",
                **{
                    "gen_ai.system": getattr(self.llm, "name", "unknown"),
                    "gen_ai.request.model": getattr(self.llm, "model", ""),
                    "session_id": state.session_id,
                    "business_id": state.business_id,
                    "n_tools": len(self.tools or []),
                    "n_messages": len(messages),
                },
            ) as _span:
                # 2026-07-31: bumped max_tokens 120 → 300.  The old 120 cap was
                # sized for Chatterbox RTF ~0.5.  Now on Cartesia (RTF ~0.15)
                # we can afford ~10s of audio.  120 was strangling the model on
                # answers longer than a sentence (e.g. "tell me about your
                # clinic") — it would tool-loop forever without producing
                # text, exhaust MAX_TOOL_ITERATIONS, and hit the teammate
                # fallback.  300 tokens ≈ ~100 words ≈ ~15s audio ≈ still
                # in-budget for a real receptionist reply.
                # 2026-08-20 (SPEED-EXTRA-B): shrunk 300 → 200.  Per OpenAI's
                # latency guide "cutting 50% of output tokens ≈ 50% of
                # total-response latency."  200 tokens ≈ 60-70 words ≈ 8-10s
                # audio, plenty for booking-confirmations while clipping the
                # LLM's tendency to over-explain.  Real over-generation
                # (rare — prompt already enforces "one to two sentences")
                # is the only case where this matters, but on those turns
                # the total-response time drops proportionally.  Prompt
                # rule remains the primary length-control mechanism.
                # ── Task #283 v2: STREAM-FIRST with tools ──────────────
                # If the caller wants streaming AND the provider supports
                # it, fire stream_complete WITH tools directly.  Peek at
                # the first chunk: text kind → tokens flow to on_delta.
                # tool_call kind → drain, build response, execute tools,
                # loop.  This kills the 2.5s batch wait on text turns.
                _stream_ok = (
                    on_delta is not None
                    and hasattr(self.llm, "stream_complete")
                )
                response = None
                _stream_full_text = ""
                _stream_tool_calls: list[ToolCall] = []
                if _stream_ok:
                    try:
                        import time as _t
                        import logging as _sl
                        _t0 = _t.perf_counter()
                        _slog = _sl.getLogger(__name__)
                        _slog.info(
                            "LLM_STREAM_START session=%s provider=%s model=%s tools=%d",
                            state.session_id,
                            getattr(self.llm, "name", "?"),
                            getattr(self.llm, "model", "?"),
                            len(self.tools or []),
                        )
                        _first_ms: Optional[float] = None
                        _tc_by_id: dict[str, dict] = {}
                        _text_chunks: list[str] = []
                        # 2026-08-20 (P3 / T-SP-SPEED-EXTRA-B2): speech-act
                        # token budget.  Previous flat 200 was over-budget for
                        # most turns (acks are ~20 tok).  Read the CURRENT
                        # turn's semantic plan (emitted on prior tool round)
                        # to pick the right cap.  Falls back to DEFAULT_BUDGET
                        # (80) when no plan present — still 60% smaller than
                        # the old flat 200.
                        from .token_budgets import token_budget_for_plan
                        _mt = token_budget_for_plan(getattr(state, "_semantic_plan", None))
                        async for _kind, _payload, _is_final in self.llm.stream_complete(
                            messages, temperature=0.3, max_tokens=_mt,
                            tools=_effective_tools,
                        ):
                            if _first_ms is None:
                                _first_ms = (_t.perf_counter() - _t0) * 1000
                                _slog.info(
                                    "LLM_FIRST_%s session=%s ms=%.0f",
                                    _kind.upper(), state.session_id, _first_ms,
                                )
                            if _kind == "text" and _payload:
                                _text_chunks.append(_payload)
                                try:
                                    await on_delta(_payload)
                                except Exception as _cbe:
                                    _slog.warning("on_delta raised: %s", _cbe)
                            elif _kind == "tool_call" and _payload:
                                tid = _payload.get("id") or f"idx{len(_tc_by_id)}"
                                _tc_by_id[tid] = _payload  # last-write-wins per id
                            if _is_final:
                                break
                        _stream_full_text = "".join(_text_chunks)
                        for _tc in _tc_by_id.values():
                            if not _tc.get("name"):
                                continue
                            try:
                                _args = json.loads(_tc.get("arguments") or "{}")
                            except json.JSONDecodeError:
                                _args = {}
                            _stream_tool_calls.append(ToolCall(
                                id=_tc.get("id") or "call_?",
                                name=_tc["name"],
                                arguments=_args,
                            ))
                        _slog.info(
                            "LLM_STREAM_DONE session=%s chars=%d tools=%d total_ms=%.0f",
                            state.session_id, len(_stream_full_text),
                            len(_stream_tool_calls),
                            (_t.perf_counter() - _t0) * 1000,
                        )
                        # Build a response-shaped object so the rest of the
                        # tool-loop code below works unchanged.
                        response = LLMResponse(
                            text=_stream_full_text,
                            tool_calls=_stream_tool_calls,
                            finish_reason="tool_calls" if _stream_tool_calls else "stop",
                            raw={},
                        )
                    except NotImplementedError:
                        # Router had no streaming provider → fall back
                        response = None
                    except Exception as _se:
                        import logging as _sl
                        _sl.getLogger(__name__).warning(
                            "stream-first path failed, falling back to batch: %s",
                            _se,
                        )
                        response = None

                if response is None:
                    # Streaming path unavailable — fall back to batch.
                    # P3: same speech-act budget applies to batch.
                    from .token_budgets import token_budget_for_plan
                    _mt = token_budget_for_plan(getattr(state, "_semantic_plan", None))
                    try:
                        response = await self.llm.complete(
                            messages, tools=_effective_tools,
                            temperature=0.3, max_tokens=_mt,
                            site="brain.reply",
                        )
                    except Exception as e:
                        _span.set_attribute("error", f"{e.__class__.__name__}: {str(e)[:200]}")
                        import logging as _logging
                        import traceback as _tb
                        _log = _logging.getLogger(__name__)
                        _log.error(
                            "LLM.complete raised %s: %s (session=%s, n_messages=%d, n_tools=%d)",
                            e.__class__.__name__, e,
                            state.session_id, len(messages), len(self.tools or []),
                            exc_info=True,
                        )
                        try:
                            from packages.observability.call_event_log import (
                                get_call_event_log, CallEvent as _CE,
                                EventSourceKind as _SK,
                            )
                            _elog = get_call_event_log()
                            if _elog is not None:
                                _elog.write(_CE(
                                    call_id=state.session_id or "?",
                                    tenant_id=getattr(state, "tenant_id", "default"),
                                    source=_SK.ERROR,
                                    kind="llm_exception",
                                    payload={
                                        "exc_class": e.__class__.__name__,
                                        "exc_message": str(e)[:500],
                                        "traceback": _tb.format_exc()[:2000],
                                        "n_messages": len(messages),
                                        "n_tools": len(self.tools or []),
                                        "site": "brain.reply",
                                        "provider": getattr(self.llm, "name", "?"),
                                        "model": getattr(self.llm, "model", "?"),
                                    },
                                    error_category="llm_provider",
                                ))
                        except Exception as _log_e:
                            _log.debug("failed to record llm_exception event: %s", _log_e)
                        fallback_text = (
                            "Sorry, I had a moment there. Could you say that again?"
                        )
                        state.add_turn(TranscriptTurn(role=TurnRole.ASSISTANT, text=fallback_text))
                        self._refresh_extraction_bg(state)
                        return BrainTurnResult(
                            reply=fallback_text,
                            state=state,
                            tool_results=tool_results_payload,
                            escalated=escalated,
                        )
                # Try to record token usage if the raw response has it (OpenAI/Anthropic shapes)
                try:
                    usage = (response.raw or {}).get("usage") or {}
                    if usage:
                        _span.set_attribute("gen_ai.usage.input_tokens", usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
                        _span.set_attribute("gen_ai.usage.output_tokens", usage.get("completion_tokens") or usage.get("output_tokens") or 0)
                        cache_read = usage.get("cache_read_input_tokens") or (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
                        _span.set_attribute("gen_ai.usage.cache_read_input_tokens", cache_read)
                except Exception:
                    pass

            # 2026-08-29 (BUG-CHR-01): LLM empty-completion watchdog.
            #
            # Christiaan (CA2fa1fef2, +31 6 25007600) hit 5 empty
            # completions in one call — LLM_STREAM_DONE chars=0 tools=0.
            # Caller sat through 8+ seconds of dead air per stall,
            # then hung up.  Content-triggered: some inputs (bare
            # nouns like 'A follow-up', Dutch phone digit strings)
            # produce empty completions on gpt-4o-mini that neither
            # the streaming path nor the batch path recovers from.
            #
            # Universal safety net: when the response has neither text
            # nor tool_calls, retry ONCE with a rephrasing prompt.
            # If the retry ALSO returns empty, fall to a deterministic
            # spoken fallback so the caller never hears dead air.
            #
            # Not a substitute for fixing the content-specific bugs
            # (BUG-CHR-02 international phone, BUG-CHR-03 service alias)
            # but every future unknown-empty gets caught here.
            _has_text = bool(response.text and response.text.strip())
            _has_tool_calls = bool(response.tool_calls)
            if not _has_text and not _has_tool_calls:
                import logging as _empty_log
                _elog_local = _empty_log.getLogger(__name__)
                _elog_local.warning(
                    "EMPTY_LLM_COMPLETION session=%s attempting rescue retry",
                    state.session_id,
                )
                # 2026-08-29 (humanness traceability): typed
                # humanness event so the trace view + evals harness
                # can query by kind="empty_llm_completion".
                try:
                    from packages.observability.humanness_events import (
                        EmptyLlmCompletionEvent, emit_humanness_event,
                    )
                    emit_humanness_event(EmptyLlmCompletionEvent(
                        call_id=state.session_id or "?",
                        tenant_id=getattr(state, "tenant_id", "default"),
                        session_id=state.session_id or "?",
                        user_text=user_text[:400],
                        site="brain.reply",
                    ))
                except Exception:
                    pass
                # Try once more with an explicit rescue system note
                # nudging the model into a short conversational reply.
                # We keep the same tool schema so a real booking tool
                # call can still emerge on the retry.
                _rescue_messages = list(messages)
                _rescue_messages.append({
                    "role": "system",
                    "content": (
                        "Your previous reply was empty. The caller last "
                        "said: " + repr(user_text)[:200] + ". Respond "
                        "briefly and conversationally in your persona. "
                        "If you need clarification, ask ONE question. "
                        "Do NOT stay silent. Do NOT emit an empty reply."
                    ),
                })
                try:
                    from .token_budgets import token_budget_for_plan
                    _mt_rescue = token_budget_for_plan(
                        getattr(state, "_semantic_plan", None),
                    )
                    response = await self.llm.complete(
                        _rescue_messages, tools=_effective_tools,
                        temperature=0.5,  # slightly warmer to unstick
                        max_tokens=_mt_rescue,
                        site="brain.rescue_empty",
                    )
                    _rescue_has_text = bool(
                        response.text and response.text.strip()
                    )
                    _rescue_has_tools = bool(response.tool_calls)
                    _elog_local.info(
                        "EMPTY_LLM_RESCUE session=%s recovered_text=%s "
                        "recovered_tools=%s",
                        state.session_id,
                        _rescue_has_text, _rescue_has_tools,
                    )
                    # 2026-08-29 (humanness traceability): typed
                    # EmptyLlmRescueEvent so trace view can render
                    # "retry succeeded / retry also empty" one-liners.
                    try:
                        from packages.observability.humanness_events import (
                            EmptyLlmRescueEvent as _ERE,
                            emit_humanness_event as _emit_hre,
                        )
                        _emit_hre(_ERE(
                            call_id=state.session_id or "?",
                            tenant_id=getattr(
                                state, "tenant_id", "default",
                            ),
                            session_id=state.session_id or "?",
                            user_text=user_text[:400],
                            recovered_text=_rescue_has_text,
                            recovered_tools=_rescue_has_tools,
                        ))
                    except Exception:
                        pass
                    # Log to the durable event log for later bisection.
                    try:
                        from packages.observability.call_event_log import (
                            get_call_event_log, CallEvent as _CE_r,
                            EventSourceKind as _SK_r,
                        )
                        _elog_durable = get_call_event_log()
                        if _elog_durable is not None:
                            _elog_durable.write(_CE_r(
                                call_id=state.session_id or "?",
                                tenant_id=getattr(
                                    state, "tenant_id", "default",
                                ),
                                source=_SK_r.LLM,
                                kind="empty_completion_rescue",
                                payload={
                                    "user_text": user_text[:400],
                                    "recovered_text": _rescue_has_text,
                                    "recovered_tools": _rescue_has_tools,
                                },
                            ))
                    except Exception:
                        pass
                except Exception as _rescue_err:
                    _elog_local.warning(
                        "EMPTY_LLM_RESCUE_FAILED session=%s: %s",
                        state.session_id, _rescue_err,
                    )
                    response = None
                # If retry ALSO failed OR returned empty, fall to a
                # deterministic spoken fallback.  Never dead air.
                if response is None or (
                    not (response.text and response.text.strip())
                    and not response.tool_calls
                ):
                    _elog_local.warning(
                        "EMPTY_LLM_DETERMINISTIC_FALLBACK session=%s",
                        state.session_id,
                    )
                    fallback_text = (
                        "Sorry, I missed that — could you say it again?"
                    )
                    # 2026-08-29 (humanness traceability): last-
                    # resort deterministic fallback fired.  Trace
                    # view marks this ERROR severity so business
                    # owners spot every fire.
                    try:
                        from packages.observability.humanness_events import (
                            EmptyLlmDeterministicFallbackEvent as _EDF,
                            emit_humanness_event as _emit_hdf,
                        )
                        _emit_hdf(_EDF(
                            call_id=state.session_id or "?",
                            tenant_id=getattr(
                                state, "tenant_id", "default",
                            ),
                            session_id=state.session_id or "?",
                            user_text=user_text[:400],
                            fallback_text=fallback_text,
                        ))
                    except Exception:
                        pass
                    state.add_turn(TranscriptTurn(
                        role=TurnRole.ASSISTANT, text=fallback_text,
                    ))
                    self._refresh_extraction_bg(state)
                    return BrainTurnResult(
                        reply=fallback_text,
                        state=state,
                        tool_results=tool_results_payload,
                        escalated=escalated,
                    )
                # else: rescue produced a real reply, continue with
                # the normal path using the new response.

            if not response.tool_calls:
                # Task #283 v2: streaming already happened above (with tools).
                # If response.text is populated, it came from either the
                # stream-first path (tokens already fired via on_delta) OR
                # a batch fallback.  Either way, sanitize and finalize.
                raw_text = response.text
                # Sanitize before speaking: strip (parentheses), <angle brackets>,
                # tool-name leakage, and expand common abbreviations. Belt-and-
                # suspenders for prompt rules the LLM sometimes ignores.
                reply_text = sanitize_for_speech(raw_text)
                # 2026-08-31 CALL-BUG-08 followup: LLM sometimes drifts the
                # agent's name mid-call ('Ava' → 'Alex'). Pin the configured
                # name post-hoc regardless of what the LLM output.
                _agent_name = getattr(self.business, "agent_name", None) or "Ava"
                reply_text = enforce_agent_name(reply_text, _agent_name)

                # 2026-08-11 (task #310): post-reply sanity check for fake
                # booking confirmations.  Hassan trace CA156d550a showed
                # the LLM saying "You're all set for your new patient exam
                # on May twelfth" WITHOUT ever calling book_appointment.
                # Booking confirmations without a tool receipt are lies to
                # the caller and create phantom-booking risk in the DB.
                # Rewrite the reply to a "still need more info" line instead.
                if _reply_lies_about_booking(reply_text, tool_results_payload):
                    import logging as _bk_log
                    _bk_log.getLogger(__name__).warning(
                        "brain: BLOCKING fake booking confirmation; rewriting reply. "
                        "original=%r tool_results=%s",
                        reply_text[:200],
                        [tr.get("name") for tr in tool_results_payload],
                    )
                    # 2026-08-30 (task #141): emit LlmClaimGuardEvent so
                    # /trace shows every claim-guard fire.  ERROR-worthy
                    # in the humanness timeline — every one is a real
                    # bug the LLM would have committed to the caller.
                    try:
                        from packages.observability.humanness_events import (
                            LlmClaimGuardEvent as _LCG,
                            emit_humanness_event as _emit_lcg,
                        )
                        _emit_lcg(_LCG(
                            call_id=state.session_id or "?",
                            tenant_id=getattr(
                                state, "tenant_id", "default",
                            ),
                            session_id=state.session_id or "?",
                            guard="booking",
                            claim_text_preview=(reply_text or "")[:200],
                            receipt_present=any(
                                (tr or {}).get("name") in
                                _BOOKING_TOOLS
                                for tr in tool_results_payload
                            ),
                            action_taken="rewrote",
                        ))
                    except Exception:
                        pass
                    reply_text = (
                        "Hold on, I don't have everything I need yet to book that. "
                        "Can you give me your full name, a full ten-digit phone number, "
                        "and the day and time you want?"
                    )

                # 2026-08-13 (R2 P0): fake-wait guard.  LLM said
                # "one moment / let me check" but no tool was ever
                # invoked this turn.  This is a lie — the caller waits
                # for something that will never come.  Killed Hamzah.
                # Rewrite to a direct clarifying question so the
                # conversation stays alive.
                if _reply_promises_wait_without_tool(
                    reply_text, tool_results_payload, response.tool_calls or [],
                ):
                    import logging as _fw_log
                    _fw_log.getLogger(__name__).warning(
                        "brain: FAKE_WAIT_BLOCKED — rewriting; "
                        "original=%r tool_calls=%d tool_results=%d",
                        reply_text[:200],
                        len(response.tool_calls or []),
                        len(tool_results_payload),
                    )
                    # 2026-08-30 (task #141): emit LlmClaimGuardEvent
                    # for the wait-promise-without-tool case.  Same
                    # trace-visibility idea as the booking guard.
                    try:
                        from packages.observability.humanness_events import (
                            LlmClaimGuardEvent as _LCG_w,
                            emit_humanness_event as _emit_lcg_w,
                        )
                        _emit_lcg_w(_LCG_w(
                            call_id=state.session_id or "?",
                            tenant_id=getattr(
                                state, "tenant_id", "default",
                            ),
                            session_id=state.session_id or "?",
                            guard="wait_promise",
                            claim_text_preview=(reply_text or "")[:200],
                            receipt_present=False,
                            action_taken="rewrote",
                        ))
                    except Exception:
                        pass
                    # 2026-08-29 (BUG #147 fix): the old fallback said
                    # "Actually, let me ask you directly — what day and
                    # time are you looking for?" which sounded jarring
                    # and prompt-leak-y ("let me ask you directly" reads
                    # like the model is quoting an instruction).  Real
                    # user complaint from live test call: "gets a bit
                    # aggressive".  Rotate through natural receptionist
                    # phrasings that keep the conversation alive without
                    # the meta-language.  Selection is deterministic on
                    # a lightweight rolling counter so consecutive fires
                    # don't sound identical.
                    _fw_fallbacks = (
                        "Sure — what day and time were you thinking?",
                        "Happy to help — what day works for you?",
                        "Yeah — when were you looking to come in?",
                        "Got it — what day and time work best?",
                        "Okay — any day in particular you had in mind?",
                    )
                    _fw_idx = getattr(state, "_fw_fallback_idx", 0)
                    reply_text = _fw_fallbacks[
                        _fw_idx % len(_fw_fallbacks)
                    ]
                    try:
                        state._fw_fallback_idx = _fw_idx + 1
                    except Exception:
                        pass

                # T-SP1 (2026-08-19): if the LLM emitted a SemanticPlan
                # this turn, substitute its critical facts into the
                # reply text.  Kills the wrong-time-substitution class
                # of bugs (LLM plans "1:30" but writes "2:30" in the
                # reply).  Non-critical facts and other drift types
                # fall through unchanged.
                _plan = getattr(state, "_semantic_plan", None)
                if _plan is not None:
                    from .plan_realizer import substitute_critical_facts
                    revised, subs = substitute_critical_facts(reply_text, _plan)
                    if subs:
                        import logging as _sp_log
                        _sp_log.getLogger(__name__).warning(
                            "SEMANTIC_PLAN_SUBSTITUTION session=%s subs=%s "
                            "original=%r revised=%r",
                            state.session_id, subs,
                            reply_text[:120], revised[:120],
                        )
                        reply_text = revised
                    # Clear so next turn doesn't reuse a stale plan.
                    state._semantic_plan = None

                state.add_turn(TranscriptTurn(role=TurnRole.ASSISTANT, text=reply_text))
                self._refresh_extraction_bg(state)
                return BrainTurnResult(
                    reply=reply_text,
                    state=state,
                    tool_results=tool_results_payload,
                    escalated=escalated,
                )

            # SPEC: when the LLM emits tool_calls we MUST persist them on the
            # assistant turn so the next round-trip serializes as a valid
            # {assistant tool_calls} → {tool result tool_call_id} pair.
            # Losing this was the root cause of the Groq 400 that killed the
            # conversation after every booking attempt.
            state.add_turn(TranscriptTurn(
                role=TurnRole.ASSISTANT,
                text=response.text.strip() if response.text else "",
                tool_calls=list(response.tool_calls),
            ))

            # Sprint 10 WIRING: kernel observes tool_call arguments and
            # records them as slot evidence.  Runs BEFORE tool_handler
            # fires so we capture even calls that later fail.
            if kernel is not None and kernel.is_enabled():
                for tc in response.tool_calls:
                    try:
                        kernel.record_slots_from_tool_call(
                            tool_name=tc.name,
                            arguments=tc.arguments or {},
                            turn_id=f"turn_{len(state.transcript)}",
                        )
                    except Exception as _e:
                        import logging as _l
                        _l.getLogger(__name__).debug(
                            "kernel.record_slots_from_tool_call failed: %s", _e,
                        )

            # T-SP1 (2026-08-19): intercept `emit_semantic_plan` FIRST
            # — it's a metadata tool, not an action.  Capture the
            # plan into state and drop the tool call from the loop so
            # the real handler doesn't see it.
            from .plan_realizer import (
                SEMANTIC_PLAN_TOOL_NAME,
                parse_semantic_plan,
            )
            _semantic_plan_tcs = [
                tc for tc in response.tool_calls
                if tc.name == SEMANTIC_PLAN_TOOL_NAME
            ]
            if _semantic_plan_tcs:
                # Take the last one if the LLM emitted more than one.
                _plan_tc = _semantic_plan_tcs[-1]
                _plan = parse_semantic_plan(_plan_tc.arguments or {})
                if _plan is not None:
                    setattr(state, "_semantic_plan", _plan)
                    import logging as _l
                    _l.getLogger(__name__).info(
                        "SEMANTIC_PLAN captured session=%s op=%s facts=%d pending=%d",
                        state.session_id,
                        _plan.operation.value,
                        len(_plan.facts),
                        len(_plan.pending_tasks),
                    )
                    # Surface pending_tasks into reactive notes so the
                    # NEXT turn's prompt can prompt about them.
                    if _plan.pending_tasks:
                        notes = list(getattr(state, "_reactive_notes", []) or [])
                        for t in _plan.pending_tasks:
                            notes.append(f"[pending_task] {t}")
                        state._reactive_notes = notes[-10:]
                # Emit a benign tool_result so the LLM sees success.
                state.add_turn(TranscriptTurn(
                    role=TurnRole.TOOL,
                    text="plan_captured",
                    tool_call_id=_plan_tc.id,
                    tool_name=_plan_tc.name,
                    tool_args=_plan_tc.arguments,
                    tool_result={"status": "captured"},
                ))
                tool_results_payload.append({
                    "name": _plan_tc.name,
                    "arguments": _plan_tc.arguments,
                    "result": {"status": "captured"},
                    "error": None,
                })
            # Continue with the REAL tool calls only.
            _real_tcs = [
                tc for tc in response.tool_calls
                if tc.name != SEMANTIC_PLAN_TOOL_NAME
            ]
            # If ALL tool calls were the metadata tool, we still fall
            # through to another LLM iteration below so the model can
            # produce a natural reply on the next round.  If there were
            # real tool calls too, they execute in the standard loop.
            for tc in _real_tcs:
                # SpeechCommitGate signal: notify BEFORE running the
                # tool.  The gate uses this to release held wait
                # promises ("one moment") the LLM emitted earlier in
                # this turn.  Callback failures never break the tool
                # loop — the caller's speech pipeline is best-effort.
                if on_tool_call is not None:
                    try:
                        await on_tool_call(tc.name)
                    except Exception as _cbe:
                        import logging as _l
                        _l.getLogger(__name__).warning(
                            "on_tool_call(%s) raised: %s", tc.name, _cbe,
                        )
                # 2026-08-30 (task #144): booking-flow continuation.
                # If discovery answers were collected earlier this
                # call (state._discovery_notes_prefix populated by
                # brain when the orchestrator completed), prepend
                # them to book_appointment(notes=) so front-desk
                # staff see procedure/provider/date context on
                # follow-up bookings.  Non-destructive: existing
                # notes are preserved after the prefix.  Only fires
                # for booking tools + only when prefix is non-empty.
                from .classifiers.write_guard import BOOKING_TOOL_NAMES, validate_write
                # 2026-08-30 (Gap 5 + task #144): booking-flow
                # continuation. Augment tool args from discovery
                # answers.  Two independent injections:
                #   (a) notes prefix — human-readable context for
                #       front-desk staff (task #144).
                #   (b) original_procedure — machine-readable slot
                #       for _service_duration() sub-typing (Gap 5).
                # Both fire on booking tools AND check_availability
                # since availability search uses duration too.
                if tc.name in BOOKING_TOOL_NAMES or tc.name == (
                    "check_availability"
                ):
                    try:
                        _args = dict(tc.arguments or {})
                        _mutated = False
                        # (a) notes prefix — booking tools only.
                        if tc.name in BOOKING_TOOL_NAMES:
                            _notes_prefix = getattr(
                                state, "_discovery_notes_prefix", "",
                            )
                            if _notes_prefix:
                                _existing_notes = str(
                                    _args.get("notes") or ""
                                ).strip()
                                if _existing_notes:
                                    _args["notes"] = (
                                        f"{_notes_prefix}. "
                                        f"{_existing_notes}"
                                    )
                                else:
                                    _args["notes"] = _notes_prefix
                                _mutated = True
                                # Consume — don't re-apply on retry.
                                try:
                                    state._discovery_notes_prefix = ""
                                except Exception:
                                    pass
                        # (b) original_procedure — booking tools OR
                        # check_availability.  Read from discovery
                        # answers (populated when orchestrator
                        # completed).  Don't overwrite if LLM already
                        # passed one.
                        _disc_answers = getattr(
                            state, "_discovery_answers", None,
                        ) or {}
                        _orig_proc = _disc_answers.get(
                            "original_procedure",
                        )
                        if _orig_proc and not _args.get(
                            "original_procedure"
                        ):
                            _args["original_procedure"] = _orig_proc
                            _mutated = True
                        if _mutated:
                            tc = tc.__class__(
                                id=tc.id,
                                name=tc.name,
                                arguments=_args,
                            )
                            import logging as _bfa_log
                            _bfa_log.getLogger(__name__).info(
                                "BOOKING_ARGS_AUGMENTED session=%s "
                                "tool=%s orig_proc=%r notes_len=%d",
                                state.session_id, tc.name,
                                (_orig_proc or "")[:40],
                                len(str(_args.get("notes") or "")),
                            )
                    except Exception as _bfa_err:
                        import logging as _bfa_log
                        _bfa_log.getLogger(__name__).warning(
                            "booking-notes augmenter failed "
                            "(falling through with original args): %s",
                            _bfa_err,
                        )
                if tc.name in BOOKING_TOOL_NAMES:
                    transcript_lines = [
                        f"{t.role.value.upper()}: {t.text}"
                        for t in state.transcript
                        if t.role in (TurnRole.USER, TurnRole.ASSISTANT) and t.text
                    ]
                    verdict = await validate_write(
                        self.extractor_llm, tc.name, tc.arguments, transcript_lines,
                    )
                    if not verdict.approved:
                        state.add_turn(TranscriptTurn(
                            role=TurnRole.TOOL,
                            text=f"BLOCKED_BY_GUARD: {verdict.reason}",
                            tool_call_id=tc.id,
                            tool_name=tc.name,
                            tool_args=tc.arguments,
                            tool_result={"blocked": True, "reason": verdict.reason, "detail": verdict.detail},
                        ))
                        state.add_turn(TranscriptTurn(role=TurnRole.ASSISTANT, text=verdict.detail))
                        tool_results_payload.append({
                            "name": tc.name,
                            "arguments": tc.arguments,
                            "result": {"blocked": True, "reason": verdict.reason},
                            "error": None,
                        })
                        # Return early — ask the caller to reconfirm rather than
                        # continuing the tool loop with the same bad args.
                        self._refresh_extraction_bg(state)
                        return BrainTurnResult(
                            reply=verdict.detail,
                            state=state,
                            tool_results=tool_results_payload,
                            escalated=escalated,
                        )

                # 2026-08-30 (task #151): intercept discovery tools
                # BEFORE they hit tool_handler.  answer_context_task
                # and regress_context_tasks are brain-internal — they
                # mutate the ContextDiscoveryOrchestrator on state,
                # not the calendar / CRM.  Returns synthetic receipt.
                _disc_intercept = None
                try:
                    if tc.name in (
                        "answer_context_task",
                        "regress_context_tasks",
                    ):
                        from packages.dialogue.context_discovery import (
                            handle_discovery_tool_call,
                        )
                        _orch = getattr(state, "_context_discovery", None)
                        # BUG #157 (Chen hallucination guard, 2026-08-31):
                        # pass recent caller utterances so the discovery
                        # handler can verify the LLM's answer_context_task
                        # arg is grounded in what the caller actually said.
                        try:
                            from .missing_slots import (
                                recent_caller_texts_from_state as _rct,
                            )
                            _disc_texts = _rct(state)
                            if user_text and user_text not in _disc_texts:
                                _disc_texts.insert(0, user_text)
                        except Exception:
                            _disc_texts = None
                        _disc_intercept = handle_discovery_tool_call(
                            _orch, tc.name, tc.arguments or {},
                            recent_caller_texts=_disc_texts,
                        )
                except Exception as _di_err:
                    import logging as _di_log
                    _di_log.getLogger(__name__).warning(
                        "discovery intercept failed (falling through): %s",
                        _di_err,
                    )
                if _disc_intercept is not None:
                    state.add_turn(TranscriptTurn(
                        role=TurnRole.TOOL,
                        text=str(_disc_intercept),
                        tool_call_id=tc.id,
                        tool_name=tc.name,
                        tool_args=tc.arguments,
                        tool_result={
                            "result": _disc_intercept, "error": None,
                        },
                    ))
                    tool_results_payload.append({
                        "name": tc.name,
                        "arguments": tc.arguments,
                        "result": _disc_intercept,
                        "error": None,
                    })
                    continue  # skip normal handler + guard
                result = await self.tool_handler(tc)
                state.add_turn(TranscriptTurn(
                    role=TurnRole.TOOL,
                    text=str(result.result),
                    tool_call_id=tc.id,
                    tool_name=tc.name,
                    tool_args=tc.arguments,
                    tool_result={"result": result.result, "error": result.error},
                ))
                tool_results_payload.append({
                    "name": tc.name,
                    "arguments": tc.arguments,
                    "result": result.result,
                    "error": result.error,
                })
                # SpeechCommitGate signal: fire AFTER receipt.  ok=True
                # iff no error AND (if result is a dict) not blocked,
                # not error-shaped, and not a precondition failure.
                # Held ACTION_CONFIRMATION sentences release only on a
                # successful matching receipt.
                if on_tool_receipt is not None:
                    _ok = result.error is None
                    if _ok and isinstance(result.result, dict):
                        # R3 P4 slim v1: phone precondition + other
                        # non-success shapes.  Any *_invalid /
                        # *_missing / *_partial / *_too_long /
                        # *_unparseable / *_ambiguous key means the
                        # tool did NOT execute the write.
                        r = result.result
                        if (
                            r.get("blocked") or r.get("error")
                            or r.get("phone_invalid") or r.get("phone_missing")
                            or r.get("phone_partial") or r.get("phone_too_long")
                            or r.get("date_unparseable") or r.get("date_ambiguous")
                        ):
                            _ok = False
                    try:
                        await on_tool_receipt(tc.name, _ok)
                    except Exception as _cbe:
                        import logging as _l
                        _l.getLogger(__name__).warning(
                            "on_tool_receipt(%s, %s) raised: %s",
                            tc.name, _ok, _cbe,
                        )
                if tc.name == "escalate_to_human":
                    escalated = True
                    state.status = CallStatus.ESCALATED
                    state.escalation_reason = str(tc.arguments.get("reason") or "caller requested human")

            # ── A1 wiring (2026-08-24): deterministic post-tool renderer ──
            #
            # The outer loop above will otherwise re-enter for a second
            # LLM iteration whose only job is to phrase the tool result
            # in natural language ("Perfect, you're booked for
            # Wednesday at nine forty-five with Doctor Chen. See you
            # then!").  For successful booking receipts we can render
            # that reply deterministically from a template + facts,
            # skipping the 2nd LLM call entirely.  See VOICE-AGENT-
            # SUB-1.5S-RD-ROADMAP-2026-08-23.md §A2 for the design +
            # expected saving (600-1200ms per booking-confirm turn).
            #
            # Guards NOT re-run on this path (by design):
            #   * _reply_lies_about_booking — synth only fires when a
            #     successful booking receipt exists, so it literally
            #     cannot fake a booking.
            #   * _reply_promises_wait_without_tool — synth produces
            #     the confirmation string, never a "one moment" wait.
            # If synth returns None we fall through to the LLM path
            # and both guards get their normal chance to fire.
            #
            # Feature-flagged: default False = zero behavior change.
            # Flip settings.next_action_policy_enabled to activate.
            _flag_on = bool(getattr(
                _brain_settings, "next_action_policy_enabled", False,
            ))
            if _flag_on and tool_results_payload:
                try:
                    from .next_action_synthesizer import (
                        maybe_synthesize,
                        maybe_synthesize_availability,
                    )
                    # Two deterministic paths this turn:
                    #   1. Booking confirmation (post book_appointment) —
                    #      the original A1/A2 wiring.
                    #   2. Availability proposal (post check_availability) —
                    #      2026-08-24 hallucination-fix wiring.  User trace:
                    #      the LLM was inventing times NOT in open_slots.
                    #      Deterministic render is the only guarantee we
                    #      speak only times the calendar actually returned.
                    known_slots = _extract_known_slots(state, tool_results_payload)
                    # BUG #158 fix write site 3: persist any tool-receipt
                    # slot values across turns.  book_appointment /
                    # book_reservation / book_viewing / check_availability
                    # all carry canonical slot args (service, date, time,
                    # phone).  Merge them into state._collected_slots so
                    # the next turn's `_persisted` seed already has them.
                    try:
                        if not hasattr(state, "_collected_slots"):
                            state._collected_slots = {}
                        # From _extract_known_slots (booking receipts only).
                        for _tk, _tv in (known_slots or {}).items():
                            if _tv:
                                state._collected_slots[_tk] = str(_tv)
                        # Also from check_availability + other slot-bearing
                        # tools — same arg schema, same canonical values.
                        for _tr in tool_results_payload or []:
                            if _tr.get("error") is not None:
                                continue
                            _args = _tr.get("arguments") or {}
                            if not isinstance(_args, dict):
                                continue
                            for _an, _av in _args.items():
                                _sk = _BOOKING_ARG_TO_SLOT.get(_an)
                                if not _sk or not _av:
                                    continue
                                if _sk == "start_iso":
                                    _iso = str(_av)
                                    if "T" in _iso:
                                        _d, _, _t = _iso.partition("T")
                                        state._collected_slots.setdefault(
                                            "date", _d.strip(),
                                        )
                                        _t2 = _t.split("+")[0].split("-")[0].split("Z")[0]
                                        state._collected_slots.setdefault(
                                            "time", _t2.strip()[:5],
                                        )
                                    else:
                                        state._collected_slots.setdefault(
                                            "date", _iso.strip(),
                                        )
                                else:
                                    state._collected_slots.setdefault(
                                        _sk, str(_av),
                                    )
                    except Exception:
                        pass
                    synth_reply, synth_skip = maybe_synthesize(
                        tool_results_payload, known_slots,
                    )
                    synth_source = "confirm_action"
                    if not synth_skip:
                        synth_reply, synth_skip = maybe_synthesize_availability(
                            tool_results_payload,
                        )
                        synth_source = "slot_proposal"
                    if synth_skip and synth_reply:
                        import logging as _synth_log
                        _synth_log.getLogger(__name__).info(
                            "brain: NEXT_ACTION_SYNTH_HIT source=%s "
                            "session=%s reply_chars=%d",
                            synth_source, state.session_id, len(synth_reply),
                        )
                        reply_text = sanitize_for_speech(synth_reply)
                        state.add_turn(TranscriptTurn(
                            role=TurnRole.ASSISTANT, text=reply_text,
                        ))
                        self._refresh_extraction_bg(state)
                        return BrainTurnResult(
                            reply=reply_text,
                            state=state,
                            tool_results=tool_results_payload,
                            escalated=escalated,
                        )
                except Exception as _synth_err:
                    import logging as _synth_log
                    _synth_log.getLogger(__name__).warning(
                        "brain: NEXT_ACTION_SYNTH_ERR (falling back to LLM): %s",
                        _synth_err,
                    )

        # 2026-07-31: loop exhausted without a text reply — most common cause
        # is the LLM tool-looping on "no_match" from lookup_faq without ever
        # committing to a plain answer.  Do ONE final non-tool call to force
        # a text reply from whatever context we have.
        try:
            messages_no_tools = [{"role": "system", "content": self.system_prompt}] + state.to_llm_messages()
            # P3: forced-final is a last resort after tool-loop exhaustion.
            # Use COMPLEX_BUDGET so the LLM has room to give a real answer
            # (this path fires when the caller asked a hard question and the
            # tool loop couldn't resolve it — under-budgeting would clip).
            from .token_budgets import COMPLEX_BUDGET
            forced = await self.llm.complete(
                messages_no_tools,
                tools=None,           # force text-only
                temperature=0.3,
                max_tokens=COMPLEX_BUDGET,
                site="brain.forced_final",
            )
            if forced.text and forced.text.strip():
                reply_text = sanitize_for_speech(forced.text)
                state.add_turn(TranscriptTurn(role=TurnRole.ASSISTANT, text=reply_text))
                self._refresh_extraction_bg(state)
                return BrainTurnResult(
                    reply=reply_text,
                    state=state,
                    tool_results=tool_results_payload,
                    escalated=escalated,
                )
        except Exception as e:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "Force-text final call failed: %s — using teammate fallback", e
            )

        fallback = "Let me have a teammate call you back to sort this out."
        state.add_turn(TranscriptTurn(role=TurnRole.ASSISTANT, text=fallback))
        self._refresh_extraction_bg(state)
        return BrainTurnResult(
            reply=fallback,
            state=state,
            tool_results=tool_results_payload,
            escalated=escalated,
        )

    def _refresh_extraction_bg(self, state: CallState) -> None:
        """2026-08-08: fire-and-forget the extractor.  2026-08-11: throttle
        to every 3rd turn.  Extractor consumes 1 LLM request per turn
        just for post-call analytics — burns free-tier rate-limit quota
        that the LIVE reply needs.  Every-3rd-turn keeps the extraction
        fresh enough for summaries without starving the router.
        The FINAL turn (call-end) is fired unconditionally by the
        session_manager so we always have final extraction.

        2026-08-21 NET: throttle bug fix.  Previous logic used
        `turn_count = len(transcript.user_turns)` which advances DURING
        a single turn as the transcript grows across the seven call
        sites in this file (emergency short-circuit, jailbreak short-
        circuit, mid tool-loop, post tool-loop, post-reply, streaming
        residual, streaming completion).  On a booking turn that adds
        the user message THEN the assistant reply THEN a follow-up
        assistant sentence, multiple call sites see different
        turn_count values — several would pass the `% 3 == 0` gate
        within ONE turn.  Result: extractor fired every real turn
        (verified in CA792b1dcf log).
        Fix: use a per-state counter that we bump ONCE per
        `handle_user_turn` entry, so the modulo check evaluates on the
        same stable value across every call site inside that turn."""
        import asyncio as _asyncio
        # Throttle: use state's turn counter (bumped once per
        # handle_user_turn entry, not derived from transcript length).
        _turn_idx = getattr(state, "_extractor_turn_idx", 0)
        if _turn_idx % 3 != 0:
            return
        # Second-line defense: dedupe within the same turn even if a
        # future refactor adds another call site that shares the same
        # `_turn_idx`.  Once the extractor has fired for a given index,
        # don't fire again for it.
        _last_fired = getattr(state, "_extractor_last_fired_idx", -1)
        if _last_fired == _turn_idx:
            return
        state._extractor_last_fired_idx = _turn_idx  # type: ignore[attr-defined]
        async def _run():
            try:
                state.extracted = await extract_fields(
                    self.extractor_llm, state.to_llm_messages(),
                )
            except Exception:
                pass
        try:
            _asyncio.create_task(_run(), name="brain-extractor-bg")
        except RuntimeError:
            # No running loop (should not happen in the actor path); swallow.
            pass

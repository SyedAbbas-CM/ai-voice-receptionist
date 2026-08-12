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
from .speech_sanitizer import sanitize_for_speech

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
    # payload AND error is None/falsy AND result isn't a blocked/error dict.
    for tr in tool_results:
        if tr.get("name") not in _BOOKING_TOOLS:
            continue
        if tr.get("error"):
            continue
        result = tr.get("result")
        if isinstance(result, dict) and (result.get("blocked") or result.get("error")):
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
        self.tools = tools
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
            parts = [f"Thanks for calling {self.business.name}, how can I help?"]
            if include_disclosure:
                parts.append("I'm an AI assistant.")
            if include_recording:
                parts.append("This call may be recorded.")
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
    ) -> BrainTurnResult:
        """on_delta: optional Callable[[str], Awaitable[None]] fired for
        each streamed token from the FINAL (no-tool-calls) LLM reply.
        The caller (twilio_actor) uses this to pipe tokens into TTS as
        sentence boundaries land. When on_delta is None or the resolved
        provider lacks stream_complete, we use the batch path."""
        state.add_turn(TranscriptTurn(role=TurnRole.USER, text=user_text))

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

        for _ in range(self.MAX_TOOL_ITERATIONS):
            messages = [{"role": "system", "content": self.system_prompt}]
            # K3+K4: inject the turn-intent hint as a fresh system note
            # ONLY for this turn (not persisted to state.transcript so
            # it doesn't pollute future turns).
            intent = getattr(state, "last_turn_intent", None)
            if intent is not None and getattr(intent, "system_note", ""):
                messages.append({"role": "system", "content": intent.system_note})
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
                        async for _kind, _payload, _is_final in self.llm.stream_complete(
                            messages, temperature=0.3, max_tokens=300,
                            tools=self.tools,
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
                    try:
                        response = await self.llm.complete(
                            messages, tools=self.tools,
                            temperature=0.3, max_tokens=300,
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
                    reply_text = (
                        "Hold on, I don't have everything I need yet to book that. "
                        "Can you give me your full name, a full ten-digit phone number, "
                        "and the day and time you want?"
                    )

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

            for tc in response.tool_calls:
                # Write-guard: validate booking tool calls against the transcript
                # before firing. Prevents the LLM from hallucinating names/phones/times
                # into the DB. Fails open on any guard error.
                from .classifiers.write_guard import BOOKING_TOOL_NAMES, validate_write
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
                if tc.name == "escalate_to_human":
                    escalated = True
                    state.status = CallStatus.ESCALATED
                    state.escalation_reason = str(tc.arguments.get("reason") or "caller requested human")

        # 2026-07-31: loop exhausted without a text reply — most common cause
        # is the LLM tool-looping on "no_match" from lookup_faq without ever
        # committing to a plain answer.  Do ONE final non-tool call to force
        # a text reply from whatever context we have.
        try:
            messages_no_tools = [{"role": "system", "content": self.system_prompt}] + state.to_llm_messages()
            forced = await self.llm.complete(
                messages_no_tools,
                tools=None,           # force text-only
                temperature=0.3,
                max_tokens=300,
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
        session_manager so we always have final extraction."""
        import asyncio as _asyncio
        # Throttle: only every 3rd turn (turn_counter % 3 == 0)
        turn_count = len([t for t in state.transcript if t.role.value == "user"])
        if turn_count % 3 != 0:
            return
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

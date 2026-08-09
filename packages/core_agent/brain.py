from __future__ import annotations

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
    from apps.api.app.providers.base import LLMProvider


ToolHandler = Callable[[ToolCall], Awaitable[ToolResult]]


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

    async def handle_user_turn(self, state: CallState, user_text: str) -> BrainTurnResult:
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
                try:
                    response = await self.llm.complete(
                        messages, tools=self.tools,
                        temperature=0.3, max_tokens=300,
                        site="brain.reply",
                    )
                except Exception as e:
                    # LLM crashed — most common cause is a Groq 400 when the
                    # 8B fallback model botches tool-call syntax (emits
                    # `<function=name>{args}` XML instead of the required
                    # JSON tool_calls array). Return a polite fallback reply
                    # so the caller isn't left staring at a spinner.
                    _span.set_attribute("error", f"{e.__class__.__name__}: {str(e)[:200]}")
                    import logging as _logging
                    _logging.getLogger(__name__).warning(
                        "LLM.complete raised %s — returning fallback reply", e
                    )
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
                # Sanitize before speaking: strip (parentheses), <angle brackets>,
                # tool-name leakage, and expand common abbreviations. Belt-and-
                # suspenders for prompt rules the LLM sometimes ignores.
                reply_text = sanitize_for_speech(response.text)
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
        """2026-08-08: fire-and-forget the extractor.  Previously we awaited
        it on the reply-return path, which added a full 2nd LLM call
        (~800-2000ms + retry cascade under rate limits) to every turn's
        end-to-end latency.  The extracted fields are only used for
        post-call analytics + summary; the LIVE reply never reads them.
        Kicking it into the background lets the reply return immediately
        and the extractor finishes whenever it finishes."""
        import asyncio as _asyncio
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

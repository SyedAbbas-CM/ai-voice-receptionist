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


class ReceptionistBrain:
    """Owns the conversation loop. One instance per call session.

    Loop per caller turn:
      1. Append caller text to transcript.
      2. Call LLM with system prompt + transcript + tools.
      3. If LLM returns tool_calls, dispatch each, append results, call LLM again.
      4. Stop when LLM returns plain text (or max_tool_iterations hit).
      5. Run a small extraction call to refresh structured fields.
    """

    MAX_TOOL_ITERATIONS = 4

    def __init__(
        self,
        llm: "LLMProvider",
        business: BusinessProfile,
        tools: list[ToolDefinition],
        tool_handler: ToolHandler,
        extractor_llm: Optional["LLMProvider"] = None,
    ) -> None:
        self.llm = llm
        self.business = business
        self.tools = tools
        self.tool_handler = tool_handler
        self.extractor_llm = extractor_llm or llm
        self.system_prompt = build_system_prompt(business)

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
            include_disclosure = getattr(self.business, "ai_disclosure_enabled", True)
            include_recording = getattr(self.business, "recording_notice_enabled", True)

            parts = [f"Hi, thanks for calling {self.business.name}."]
            if include_disclosure:
                parts.append("I'm an AI assistant here to help.")
            if include_recording:
                parts.append("This call may be recorded for quality.")
            parts.append("How can I help you today?")
            greeting = " ".join(parts)

        state.add_turn(TranscriptTurn(role=TurnRole.ASSISTANT, text=greeting))
        return BrainTurnResult(reply=greeting, state=state)

    async def handle_user_turn(self, state: CallState, user_text: str) -> BrainTurnResult:
        state.add_turn(TranscriptTurn(role=TurnRole.USER, text=user_text))

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
            await self._refresh_extraction(state)
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
            await self._refresh_extraction(state)
            return BrainTurnResult(reply=safe_reply, state=state)

        tool_results_payload: list[dict] = []
        escalated = False

        from packages.observability import get_tracer
        tracer = get_tracer()

        for _ in range(self.MAX_TOOL_ITERATIONS):
            messages = [{"role": "system", "content": self.system_prompt}] + state.to_llm_messages()
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
                    await self._refresh_extraction(state)
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
                await self._refresh_extraction(state)
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
                        await self._refresh_extraction(state)
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
            )
            if forced.text and forced.text.strip():
                reply_text = sanitize_for_speech(forced.text)
                state.add_turn(TranscriptTurn(role=TurnRole.ASSISTANT, text=reply_text))
                await self._refresh_extraction(state)
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
        await self._refresh_extraction(state)
        return BrainTurnResult(
            reply=fallback,
            state=state,
            tool_results=tool_results_payload,
            escalated=escalated,
        )

    async def _refresh_extraction(self, state: CallState) -> None:
        try:
            state.extracted = await extract_fields(self.extractor_llm, state.to_llm_messages())
        except Exception:
            pass

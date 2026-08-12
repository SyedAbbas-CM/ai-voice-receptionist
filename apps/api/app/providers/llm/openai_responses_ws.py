"""Persistent OpenAI Responses WebSocket per call (N1).

Motivation (2026-08-13, task #344):
  From PK, every LLM turn today opens a fresh HTTPS/2 stream to
  api.openai.com.  Even with a warmed httpx client we still pay one
  full PK->US RTT for the request/response envelope, and the entire
  ~1500-token system+tools prefix is re-transmitted every turn.

  OpenAI's Responses API supports a persistent WebSocket
  (`wss://api.openai.com/v1/responses`).  Connection-local state
  makes it possible to:
    1. warm the socket during the greeting playback with the exact
       tenant prompt + tool schemas (no user turn yet)
    2. on the first real user turn, send only the new user text —
       no HTTPS handshake, no full prefix re-transmission
    3. keep the socket open for the whole call, reusing it for
       every subsequent turn

  Scoped to ONE call at a time.  On any protocol / connectivity /
  parsing error we log + close + let the caller fall back to the
  HTTP router (which is still the safety net for tool-call turns).

Wire protocol summary (from the guide + probing):
  outbound:
    {"type": "response.create",
     "model": "...",
     "store": false,
     "input": [{"type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "..."}]}],
     "tools": [...]}
  inbound events (subset we care about — others logged + ignored):
    response.created
    response.output_text.delta   {delta: str}
    response.output_text.done    {text: str}
    response.function_call_arguments.delta
    response.function_call_arguments.done
    response.output_item.added   (may carry function_call metadata)
    response.output_item.done
    response.completed           {response: {usage: ..., ...}}
    response.failed / response.error

Not yet exercised:
  - `generate: false` warmup: docs mention it exists but don't spell
    out the wire schema.  We warm by sending a real generation with
    max_output_tokens=1 instead — same effect (socket + state alive)
    without waiting on the spec.
  - service_tier="fast": we pass it if configured, ignored server-
    side otherwise.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Awaitable, Callable, Optional

import websockets
from websockets.protocol import State

from app.core.config import settings
from packages.schemas import ToolCall, ToolDefinition

log = logging.getLogger(__name__)


# Events we surface to callers.  Everything else is logged at DEBUG.
_KNOWN_TEXT_DELTA = "response.output_text.delta"
_KNOWN_TEXT_DONE = "response.output_text.done"
_KNOWN_COMPLETED = "response.completed"
# 2026-08-13 (verified via live probe): OpenAI emits `type: "error"`
# at the top level for validation failures (not `response.error`).
# `response.failed` also exists for mid-generation failures.
_KNOWN_ERROR_TOP = "error"
_KNOWN_FAILED = "response.failed"
_KNOWN_ERROR = "response.error"
_KNOWN_FN_ARGS_DELTA = "response.function_call_arguments.delta"
_KNOWN_FN_ARGS_DONE = "response.function_call_arguments.done"
_KNOWN_OUTPUT_ITEM_ADDED = "response.output_item.added"

# 2026-08-13 (verified via live probe): OpenAI requires max_output_tokens
# >= 16.  Passing 1 returns HTTP 400.
_MIN_MAX_OUTPUT_TOKENS = 16


class OpenAIResponsesWS:
    """Per-call persistent WebSocket to OpenAI's Responses API.

    Lifecycle:
        ws = OpenAIResponsesWS(call_id="CAxxx")
        await ws.open(system_prompt=..., tools=..., warm=True)
        # ...greeting plays concurrently...
        reply_text, tool_calls = await ws.stream_reply(
            user_text="What implants do you offer?",
            on_delta=my_callback,
        )
        await ws.close()

    Not thread-safe; owned by one call actor only.
    """

    def __init__(self, call_id: str) -> None:
        self.call_id = call_id
        self.api_key = settings.openai_api_key
        self.url = settings.openai_persistent_ws_url
        self.model = (
            settings.openai_persistent_ws_model
            or settings.openai_model
            or "gpt-4o-mini"
        )
        self.service_tier = settings.openai_persistent_ws_service_tier or None

        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._open_wall_ms: Optional[float] = None
        self._system_prompt: Optional[str] = None
        self._tools_openai: Optional[list[dict]] = None
        # Fresh event queue per in-flight response.create.  Set on
        # stream_reply entry, consumed by _pump_events.
        self._inflight_lock = asyncio.Lock()

    # ── lifecycle ────────────────────────────────────────────────────

    async def open(
        self,
        system_prompt: str,
        tools: Optional[list[ToolDefinition]] = None,
        *,
        warm: bool = True,
    ) -> bool:
        """Establish the WS + optionally warm it.

        Returns True on success, False on any connect / warm failure —
        caller falls back to HTTP router.
        """
        if not self.api_key:
            log.warning("OPENAI_PERSISTENT_WS_OPEN_SKIP call=%s reason=no_api_key",
                        self.call_id)
            return False
        self._system_prompt = system_prompt
        # 2026-08-13 (verified via live probe): Responses API uses FLAT
        # tool schema, NOT the Chat Completions nested {"type":"function",
        # "function":{...}} shape.  Fields go at the top level of the
        # tool object.
        self._tools_openai = [
            {
                "type": "function",
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            }
            for t in (tools or [])
        ]

        connect_start = time.perf_counter()
        try:
            self._ws = await websockets.connect(
                self.url,
                additional_headers=[("Authorization", f"Bearer {self.api_key}")],
                # Keep the socket alive; OpenAI docs say connection
                # duration is capped at 60 minutes.
                ping_interval=20,
                ping_timeout=10,
                close_timeout=2,
                max_size=8 * 1024 * 1024,
            )
        except Exception as e:
            log.warning(
                "OPENAI_PERSISTENT_WS_OPEN_FAIL call=%s err=%s: %s",
                self.call_id, e.__class__.__name__, str(e)[:200],
            )
            self._ws = None
            return False
        self._open_wall_ms = time.perf_counter() - connect_start
        log.info(
            "OPENAI_PERSISTENT_WS_OPEN call=%s model=%s took_ms=%d",
            self.call_id, self.model, int(self._open_wall_ms * 1000),
        )
        if not warm:
            return True
        # Fire-and-forget warmup so start() returns fast.  If it fails,
        # the real turn will just pay first-turn cost — no correctness
        # impact.
        try:
            await self._warm_state()
        except Exception:
            log.exception("OPENAI_PERSISTENT_WS_WARM_FAIL call=%s", self.call_id)
            # Warm failed but socket is still up; return True so the
            # real turn can still try it.
        return True

    async def _warm_state(self) -> None:
        """Send a tiny generation to prime prefix cache + connection.

        Uses max_output_tokens=1 with a synthetic user "ping" so the
        server sees the full system + tools prefix and caches it.  We
        drain events but discard the tiny output.
        """
        if self._ws is None or self._system_prompt is None:
            return
        warm_start = time.perf_counter()
        # Minimum accepted is 16 tokens; keep it exactly there so we
        # do the smallest possible generation while still priming the
        # server-side prefix cache on our exact system+tools prompt.
        request = self._build_response_create(
            user_text="ping",
            max_output_tokens=_MIN_MAX_OUTPUT_TOKENS,
        )
        await self._send_json(request)
        # Drain until response.completed (or failed).  Cap at 6s so a
        # hung warmup doesn't stall the call actor path.
        try:
            await asyncio.wait_for(
                self._drain_until_completed(collect_deltas=False),
                timeout=6.0,
            )
        except asyncio.TimeoutError:
            log.warning("OPENAI_PERSISTENT_WS_WARM_TIMEOUT call=%s", self.call_id)
            return
        warm_ms = int((time.perf_counter() - warm_start) * 1000)
        log.info(
            "OPENAI_PERSISTENT_WS_WARM call=%s took_ms=%d",
            self.call_id, warm_ms,
        )

    async def close(self) -> None:
        ws = self._ws
        self._ws = None
        if ws is None:
            return
        try:
            await asyncio.wait_for(ws.close(), timeout=1.5)
        except Exception:
            pass
        log.info("OPENAI_PERSISTENT_WS_CLOSE call=%s", self.call_id)

    def is_open(self) -> bool:
        return self._ws is not None and self._ws.state == State.OPEN

    # ── turn dispatch ────────────────────────────────────────────────

    async def stream_reply(
        self,
        user_text: str,
        on_delta: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> tuple[str, list[ToolCall]]:
        """Send a user turn, stream deltas, return (full_text, tool_calls).

        Raises RuntimeError on any protocol / connectivity issue so the
        caller can fall back to the HTTP router.  Serialized via
        _inflight_lock — Responses WS allows only one in-flight response
        per connection.
        """
        if not self.is_open():
            raise RuntimeError("openai_persistent_ws not open")

        async with self._inflight_lock:
            request = self._build_response_create(user_text=user_text)
            send_wall = time.perf_counter()
            await self._send_json(request)
            first_delta_wall: Optional[float] = None
            full_parts: list[str] = []
            tool_calls: list[ToolCall] = []
            # Function-call accumulation keyed by output-item id.  The
            # Responses API streams call name + arguments piecewise.
            fn_acc: dict[str, dict] = {}

            async def on_event(evt: dict) -> None:
                nonlocal first_delta_wall
                etype = evt.get("type", "")
                if etype == _KNOWN_TEXT_DELTA:
                    delta = evt.get("delta") or ""
                    if delta:
                        if first_delta_wall is None:
                            first_delta_wall = time.perf_counter()
                            log.info(
                                "OPENAI_PERSISTENT_WS_TTFT call=%s ttft_ms=%d",
                                self.call_id,
                                int((first_delta_wall - send_wall) * 1000),
                            )
                        full_parts.append(delta)
                        if on_delta is not None:
                            try:
                                await on_delta(delta)
                            except Exception:
                                log.exception(
                                    "on_delta callback raised call=%s",
                                    self.call_id,
                                )
                elif etype == _KNOWN_OUTPUT_ITEM_ADDED:
                    item = evt.get("item") or {}
                    if item.get("type") == "function_call":
                        item_id = str(item.get("id") or item.get("call_id") or "")
                        if item_id:
                            fn_acc[item_id] = {
                                "id": item.get("call_id") or item_id,
                                "name": item.get("name") or "",
                                "arguments": "",
                            }
                elif etype == _KNOWN_FN_ARGS_DELTA:
                    item_id = str(evt.get("item_id") or "")
                    if item_id in fn_acc:
                        fn_acc[item_id]["arguments"] += evt.get("delta", "")
                elif etype == _KNOWN_FN_ARGS_DONE:
                    item_id = str(evt.get("item_id") or "")
                    if item_id in fn_acc:
                        # Some deployments only send done + full args
                        args_full = evt.get("arguments")
                        if args_full and not fn_acc[item_id]["arguments"]:
                            fn_acc[item_id]["arguments"] = args_full

            await self._drain_until_completed(
                collect_deltas=True, on_event=on_event,
            )

            for acc in fn_acc.values():
                if not acc.get("name"):
                    continue
                args_raw = acc.get("arguments") or "{}"
                try:
                    args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                except json.JSONDecodeError:
                    log.warning(
                        "OPENAI_PERSISTENT_WS_TOOL_ARGS_PARSE_FAIL call=%s name=%s args=%r",
                        self.call_id, acc.get("name"), args_raw[:200],
                    )
                    args = {}
                tool_calls.append(ToolCall(
                    id=acc.get("id") or "",
                    name=acc["name"],
                    arguments=args if isinstance(args, dict) else {"raw": args},
                ))
            return "".join(full_parts), tool_calls

    # ── internals ────────────────────────────────────────────────────

    def _build_response_create(
        self,
        user_text: str,
        max_output_tokens: Optional[int] = None,
    ) -> dict:
        """Compose one response.create request.

        Includes system prompt + tools every time.  If OpenAI's server
        caches on the persistent connection this is redundant but
        harmless; if it doesn't, we still get correct behavior.  Prompt-
        prefix caching on the account side is byte-stable so this
        maximizes cache hits.
        """
        # Responses API input schema — system as instructions, user as
        # a message.  Keep instructions field separate so it lives on
        # the connection state where the server expects it.
        req: dict = {
            "type": "response.create",
            "model": self.model,
            "store": False,
            "instructions": self._system_prompt or "",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": user_text},
                    ],
                }
            ],
        }
        if self._tools_openai:
            req["tools"] = self._tools_openai
        if max_output_tokens is not None:
            req["max_output_tokens"] = max_output_tokens
        if self.service_tier:
            req["service_tier"] = self.service_tier
        return req

    async def _send_json(self, obj: dict) -> None:
        assert self._ws is not None
        await self._ws.send(json.dumps(obj))

    async def _drain_until_completed(
        self,
        *,
        collect_deltas: bool,
        on_event: Optional[Callable[[dict], Awaitable[None]]] = None,
    ) -> None:
        """Read events off the WS until response.completed/failed/error.

        Any protocol error → close + raise RuntimeError so caller can
        fall back to HTTP.
        """
        assert self._ws is not None
        while True:
            try:
                raw = await self._ws.recv()
            except websockets.ConnectionClosed as e:
                log.warning(
                    "OPENAI_PERSISTENT_WS_CLOSED_MID_TURN call=%s code=%s",
                    self.call_id, getattr(e, "code", "?"),
                )
                self._ws = None
                raise RuntimeError(f"ws closed: {e}") from e
            try:
                evt = json.loads(raw)
            except json.JSONDecodeError:
                log.warning(
                    "OPENAI_PERSISTENT_WS_BAD_JSON call=%s raw=%r",
                    self.call_id, str(raw)[:200],
                )
                continue
            etype = evt.get("type", "")
            if etype in (_KNOWN_FAILED, _KNOWN_ERROR, _KNOWN_ERROR_TOP):
                # Top-level "error" (validation) does NOT close the WS,
                # so if the caller wants to try another turn they can.
                # Bubble up so this turn fails and the actor path falls
                # back to HTTP.
                err_detail = evt.get("error") or {}
                log.warning(
                    "OPENAI_PERSISTENT_WS_SERVER_ERR call=%s type=%s err=%s",
                    self.call_id, etype,
                    (err_detail.get("message") or json.dumps(evt))[:400],
                )
                raise RuntimeError(f"openai ws server error: {etype}")
            if etype == _KNOWN_COMPLETED:
                # Log token usage + cache hit if the server sent it.
                usage = ((evt.get("response") or {}).get("usage") or {})
                cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
                if usage:
                    log.info(
                        "OPENAI_PERSISTENT_WS_COMPLETED call=%s "
                        "prompt_tokens=%s output_tokens=%s cached_tokens=%s",
                        self.call_id,
                        usage.get("input_tokens") or usage.get("prompt_tokens"),
                        usage.get("output_tokens") or usage.get("completion_tokens"),
                        cached,
                    )
                return
            if on_event is not None and collect_deltas:
                await on_event(evt)
            elif etype in (
                _KNOWN_TEXT_DELTA,
                _KNOWN_TEXT_DONE,
                _KNOWN_FN_ARGS_DELTA,
                _KNOWN_FN_ARGS_DONE,
                _KNOWN_OUTPUT_ITEM_ADDED,
            ):
                # Warmup path — deliberately dropping deltas.
                pass
            else:
                # Log unknown events at debug once so we can iterate on
                # the wire schema.
                log.debug(
                    "OPENAI_PERSISTENT_WS_EVT call=%s type=%s",
                    self.call_id, etype,
                )

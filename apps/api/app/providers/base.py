from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator, Optional

from pydantic import BaseModel

from packages.schemas import ToolCall, ToolDefinition


class LLMResponse(BaseModel):
    text: str
    tool_calls: list[ToolCall] = []
    finish_reason: str = "stop"
    raw: Optional[dict] = None


class LLMProvider(ABC):
    """Cloud or local LLM. Must support tool calling for the receptionist brain.

    Structured output contract (2026-08-07):
        Any provider MAY accept a `response_schema` kwarg (pydantic
        model class OR JSON Schema dict).  Adapters translate to the
        native mode of their family:
          - OpenAI-compat: response_format={"type":"json_schema", "strict":true}
          - Anthropic:      forced tool_use with the schema as the tool
          - Gemini:         generation_config.response_schema
        Adapters that don't support strict schema fall back to loose
        json_object mode.  Adapters that don't support even that raise
        NotImplementedError; the router capability gate should never
        route a response_schema request to such a model.
    """

    name: str = "base"

    @abstractmethod
    async def complete(
        self,
        messages: list[dict],
        tools: Optional[list[ToolDefinition]] = None,
        temperature: float = 0.3,
        max_tokens: int = 1024,
        response_schema: Optional[object] = None,
    ) -> LLMResponse:
        ...

    async def stream(
        self,
        messages: list[dict],
        tools: Optional[list[ToolDefinition]] = None,
        temperature: float = 0.3,
    ) -> AsyncIterator[str]:
        """Optional streaming. Default: yield the full completion once."""
        response = await self.complete(messages, tools, temperature)
        yield response.text

    async def stream_complete(
        self,
        messages: list[dict],
        temperature: float = 0.3,
        max_tokens: int = 1024,
        tools: Optional[list[ToolDefinition]] = None,
    ):
        """Task #283 v2: yields (kind, payload, is_final) tuples.

        kind="text" → payload is a str delta.
        kind="tool_call" → payload is a partial tool-call dict:
                          {id, name, arguments}
        Providers opt in by overriding.  Optional `tools` lets the
        caller stream WITH tools so the first chunks tell us which
        branch the model chose (text vs tool_call) — that's the whole
        early-cancel design.
        """
        raise NotImplementedError(
            f"{self.name} does not support token-level streaming",
        )
        yield  # pragma: no cover — makes this an async generator


class STTEvent(BaseModel):
    """Streaming STT emits a sequence of these.

    - kind="partial" — current hypothesis for the utterance in progress.
      Text updates rapidly. Use to trigger barge-in the moment the caller
      starts speaking; DO NOT commit to the brain yet.
    - kind="final" — text is stable and won't be revised.  This may be a
      mid-sentence endpoint (Deepgram is_final=True but speech_final=False)
      OR a real utterance end (speech_final=True).  Turn manager should
      distinguish: speech_final=True is safe to promote to END_OF_TURN;
      is_final-only should be buffered until speech_final arrives.
    - kind="speech_start" — VAD detected voice; text will be empty.
    - kind="speech_end" — VAD detected end of voice.
    - kind="eager_end_of_turn" — 2026-08-11 Deepgram Flux only.  Model
      thinks caller is done but hasn't committed yet — safe to fire
      speculative brain, cancel if TurnResumed follows.
    - kind="end_of_turn" — Deepgram Flux only.  Model confirmed the
      turn is complete.  Bypasses our 400ms confirm window.
    - kind="turn_resumed" — Deepgram Flux only.  Cancel any speculative
      brain fired on eager_end_of_turn.
    """
    kind: str  # partial | final | speech_start | speech_end | eager_end_of_turn | end_of_turn | turn_resumed
    text: str = ""
    is_final: bool = False
    speech_final: bool = False   # True when VAD confirmed real end-of-utterance


class STTProvider(ABC):
    """Speech-to-text. Used by browser sim and phone webhook transports."""

    name: str = "base"
    supports_streaming: bool = False

    @abstractmethod
    async def transcribe(self, audio_bytes: bytes, sample_rate: int = 16000, mime: str = "audio/wav") -> str:
        ...

    async def transcribe_stream(
        self,
        audio_chunks: AsyncIterator[bytes],
        sample_rate: int = 16000,
        encoding: str = "linear16",
        keyterms: Optional[list[str]] = None,
    ) -> AsyncIterator[STTEvent]:
        """Optional streaming STT. Default raises so callers can detect lack of support.

        `audio_chunks` is an async iterator of raw PCM16 (or µ-law if
        encoding='mulaw') frames from the transport. Yields STTEvent
        objects. Callers should route partials to a barge-in detector
        and finals to the brain.

        `keyterms` (LK-steal T3, 2026-08-30): optional per-connection
        boost terms — tenant name, service list, staff names, etc.
        Providers that don't support keyterm boosting should ignore it
        rather than error, so callers can pass it unconditionally.
        `None` = don't send the param at all (some providers, notably
        Deepgram Nova-3 vs Flux, differ on whether to still send the
        provider's baseline hardcoded set as a fallback)."""
        raise NotImplementedError(f"{self.name} does not support streaming STT")
        yield  # pragma: no cover — makes this an async generator for type checkers


class TTSProvider(ABC):
    """Text-to-speech. Returns raw audio bytes plus a content type."""

    name: str = "base"

    # True when the provider overrides stream_sentences() with a truly
    # streaming implementation (e.g. Chatterbox --stream, ElevenLabs SSE).
    # False providers fall back to sequential per-sentence synth via the
    # default stream_sentences below. Callers check this only for logging;
    # the interface behaves identically either way.
    supports_streaming: bool = False

    @abstractmethod
    async def synthesize(self, text: str, voice: Optional[str] = None) -> tuple[bytes, str]:
        """Returns (audio_bytes, mime_type)."""
        ...

    async def stream_sentences(
        self, text: str, voice: Optional[str] = None,
    ):
        """Yield (audio_bytes, mime) for each sentence chunk as it becomes ready.

        Default implementation: split text into sentence chunks, synthesize
        each SEQUENTIALLY, yield each result. This gives the CALLER useful
        progressive playback (first sound arrives at ~sentence-1-latency
        instead of full-reply-latency) without requiring the provider to
        support true token-level streaming.

        Providers that CAN stream natively should override this to do so —
        e.g. streaming WebSocket audio frames from the model directly.
        """
        from packages.voice.sentence_splitter import split_into_speakable_chunks
        chunks = split_into_speakable_chunks(text)
        for chunk in chunks:
            data, mime = await self.synthesize(chunk, voice=voice)
            yield data, mime


class TransportProvider(ABC):
    """Transport layer: browser WebRTC, Twilio Media Streams, LiveKit, Vapi webhook."""

    name: str = "base"

    @abstractmethod
    async def start_session(self, session_id: str) -> dict:
        ...

    @abstractmethod
    async def end_session(self, session_id: str) -> None:
        ...

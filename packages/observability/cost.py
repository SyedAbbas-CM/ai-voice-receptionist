"""Per-call cost accounting.

Multiplies observed usage numbers (tokens, seconds, characters) by a
rate card to produce a $ estimate you can attach to any span. Rates are
2026 published prices; each app can override the CostBook via env.

Numbers are USD. Estimates are close enough to argue with a client about
architecture but shouldn't be used for actual billing — pull real usage
from provider dashboards for that.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class CostRate:
    """Per-provider, per-model rate row."""
    provider: str
    model: str
    input_per_million: float = 0.0    # LLM input tokens ($/M)
    output_per_million: float = 0.0   # LLM output tokens ($/M)
    cache_read_per_million: float = 0.0  # cached input ($/M)
    per_minute: float = 0.0            # STT $/min, TTS $/min
    per_million_chars: float = 0.0     # TTS $/M chars


@dataclass
class CostBook:
    llm: dict[str, CostRate] = field(default_factory=dict)
    stt: dict[str, CostRate] = field(default_factory=dict)
    tts: dict[str, CostRate] = field(default_factory=dict)

    def llm_rate(self, provider: str, model: str) -> CostRate:
        return self.llm.get(f"{provider}/{model}") or self.llm.get(provider) or CostRate(provider=provider, model=model)

    def stt_rate(self, provider: str) -> CostRate:
        return self.stt.get(provider) or CostRate(provider=provider, model="")

    def tts_rate(self, provider: str) -> CostRate:
        return self.tts.get(provider) or CostRate(provider=provider, model="")


DEFAULT_COST_BOOK = CostBook(
    llm={
        "openai/gpt-4o-mini": CostRate("openai", "gpt-4o-mini", 0.15, 0.60, 0.075),
        "openai/gpt-4o": CostRate("openai", "gpt-4o", 2.50, 10.00, 1.25),
        "anthropic/claude-sonnet-4-6": CostRate("anthropic", "claude-sonnet-4-6", 3.00, 15.00, 0.30),
        "groq/llama-3.3-70b-versatile": CostRate("groq", "llama-3.3-70b-versatile", 0.59, 0.79, 0),
        "groq/llama-3.1-8b-instant": CostRate("groq", "llama-3.1-8b-instant", 0.05, 0.08, 0),
        "cerebras/gpt-oss-120b": CostRate("cerebras", "gpt-oss-120b", 0.35, 1.35, 0),
        "nvidia/meta/llama-3.1-70b-instruct": CostRate("nvidia", "meta/llama-3.1-70b-instruct", 0.20, 0.20, 0),
        "openrouter/meta-llama/llama-3.3-70b-instruct": CostRate("openrouter", "meta-llama/llama-3.3-70b-instruct", 0.13, 0.40, 0),
        "gemini/gemini-2.5-flash": CostRate("gemini", "gemini-2.5-flash", 0.30, 2.50, 0.075),
        # local providers: $0 by definition
        "ollama": CostRate("ollama", "*", 0, 0, 0),
        "local": CostRate("local", "*", 0, 0, 0),
    },
    stt={
        "deepgram": CostRate("deepgram", "nova-3", per_minute=0.0043),
        "openai": CostRate("openai", "whisper-1", per_minute=0.006),
        "groq": CostRate("groq", "whisper-large-v3-turbo", per_minute=0.04 / 60),  # ~$0.04/hr
        "local": CostRate("local", "faster-whisper", per_minute=0),
    },
    tts={
        "elevenlabs": CostRate("elevenlabs", "eleven_turbo_v2_5", per_million_chars=180),  # ~$0.18/1k chars
        "openai": CostRate("openai", "gpt-4o-mini-tts", per_million_chars=15),  # ~$0.015/1k
        "deepgram": CostRate("deepgram", "aura-asteria-en", per_million_chars=15),
        "cartesia": CostRate("cartesia", "sonic-2", per_million_chars=65),
        "qwen3": CostRate("qwen3", "0.6B-CustomVoice", per_million_chars=0),  # local
        "kokoro": CostRate("kokoro", "82M", per_million_chars=0),  # local
        "local": CostRate("local", "piper", per_million_chars=0),
        "browser": CostRate("browser", "SpeechSynthesis", per_million_chars=0),
    },
)


def estimate_llm_cost(
    provider: str, model: str,
    input_tokens: int, output_tokens: int, cache_read_tokens: int = 0,
    book: CostBook = DEFAULT_COST_BOOK,
) -> float:
    """Returns USD estimate for a single LLM call.

    Cached tokens count against `cache_read_per_million`, not the full input
    rate — this is the whole point of prompt caching accounting."""
    rate = book.llm_rate(provider, model)
    fresh_input = max(0, input_tokens - cache_read_tokens)
    return (
        (fresh_input / 1_000_000) * rate.input_per_million
        + (cache_read_tokens / 1_000_000) * rate.cache_read_per_million
        + (output_tokens / 1_000_000) * rate.output_per_million
    )


def estimate_stt_cost(provider: str, seconds: float, book: CostBook = DEFAULT_COST_BOOK) -> float:
    """USD estimate for STT of `seconds` of audio."""
    rate = book.stt_rate(provider)
    return (seconds / 60) * rate.per_minute


def estimate_tts_cost(provider: str, characters: int, book: CostBook = DEFAULT_COST_BOOK) -> float:
    """USD estimate for TTS of `characters` of text."""
    rate = book.tts_rate(provider)
    return (characters / 1_000_000) * rate.per_million_chars

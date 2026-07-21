from __future__ import annotations

from functools import lru_cache

from app.core.config import settings
from .base import LLMProvider, STTProvider, TTSProvider


@lru_cache(maxsize=1)
def get_llm() -> LLMProvider:
    provider = settings.llm_provider.lower()
    if provider == "openai":
        from .llm.openai_llm import OpenAILLM
        return OpenAILLM()
    if provider == "anthropic":
        from .llm.anthropic_llm import AnthropicLLM
        return AnthropicLLM()
    if provider == "groq":
        from .llm.groq_llm import GroqLLM
        return GroqLLM()
    if provider == "gemini":
        from .llm.gemini_llm import GeminiLLM
        return GeminiLLM()
    if provider == "ollama":
        from .llm.ollama_llm import OllamaLLM
        return OllamaLLM()
    if provider == "cerebras":
        from .llm.cerebras_llm import CerebrasLLM
        return CerebrasLLM()
    if provider == "openrouter":
        from .llm.openrouter_llm import OpenRouterLLM
        return OpenRouterLLM()
    if provider in ("nvidia", "nim", "nvidia_nim"):
        from .llm.nvidia_nim_llm import NvidiaNimLLM
        return NvidiaNimLLM()
    raise ValueError(f"Unknown LLM provider: {provider}")


@lru_cache(maxsize=1)
def get_stt() -> STTProvider:
    provider = settings.stt_provider.lower()
    if provider == "deepgram":
        from .stt.deepgram_stt import DeepgramSTT
        return DeepgramSTT()
    if provider == "openai":
        from .stt.openai_stt import OpenAISTT
        return OpenAISTT()
    if provider == "groq":
        from .stt.groq_stt import GroqSTT
        return GroqSTT()
    if provider == "local":
        from .stt.local_whisper_stt import LocalWhisperSTT
        return LocalWhisperSTT()
    raise ValueError(f"Unknown STT provider: {provider}")


@lru_cache(maxsize=1)
def get_tts() -> TTSProvider:
    provider = settings.tts_provider.lower()
    if provider == "elevenlabs":
        from .tts.elevenlabs_tts import ElevenLabsTTS
        return ElevenLabsTTS()
    if provider == "openai":
        from .tts.openai_tts import OpenAITTS
        return OpenAITTS()
    if provider == "deepgram":
        from .tts.deepgram_tts import DeepgramTTS
        return DeepgramTTS()
    if provider == "cartesia":
        from .tts.cartesia_tts import CartesiaTTS
        return CartesiaTTS()
    if provider == "local":
        from .tts.local_piper_tts import LocalPiperTTS
        return LocalPiperTTS()
    if provider == "qwen3":
        from .tts.qwen3_tts import Qwen3TTS
        return Qwen3TTS()
    if provider == "kokoro":
        from .tts.kokoro_tts import KokoroTTS
        return KokoroTTS()
    if provider == "chatterbox":
        from .tts.chatterbox_mlx_tts import ChatterboxMLX
        return ChatterboxMLX()
    if provider == "browser":
        from .tts.browser_tts import BrowserTTS
        return BrowserTTS()
    raise ValueError(f"Unknown TTS provider: {provider}")

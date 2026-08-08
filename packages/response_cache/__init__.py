"""Response cache: skip brain+TTS entirely for repeat questions.

When a caller says "are you open Saturdays?" for the 50th time, we've
already computed the perfect reply text AND synthesized the µ-law audio.
The response cache stores (input_hash → reply_text + tts_cache_key)
per business_id so the next matching turn plays instantly.

Cache layers:
    Level 1 (this pkg): input_hash → reply metadata
    Level 2 (packages/tts_cache): tts_cache_key → µ-law bytes

Miss path is unchanged: brain runs, TTS synthesizes, both caches populated.
Hit path bypasses brain + TTS entirely — 100-200ms end-to-end.
"""
from .cache import ResponseCache, get_shared_response_cache, normalize_input

__all__ = ["ResponseCache", "get_shared_response_cache", "normalize_input"]

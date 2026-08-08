"""Task A: TTS speech-act cache.

Wraps any TTSProvider (ElevenLabs, Cartesia, ...) with a hash-keyed
disk cache.  Every synth request is looked up by hash(voice + text +
format).  Cache hit → disk read (~5-10ms), skip provider entirely.
Cache miss → provider call, store the result, return.

Observability:
    voiceops_tts_cache_lookups_total{result="hit|miss|error"}
    voiceops_tts_cache_stores_total
    voiceops_tts_cache_hit_latency_seconds
    voiceops_tts_cache_miss_latency_seconds
    voiceops_tts_cache_size_bytes
    voiceops_tts_cache_entries

Persistent across restarts (data/tts_cache/).  LRU eviction when the
cache dir exceeds max_bytes.

Design principles:
- ZERO risk: cache miss = original behaviour.  Wrapper never swallows
  provider errors, never returns stale audio, never blocks the miss
  path on the write.
- Text normalisation is minimal: lowercase + strip trailing punct +
  collapse whitespace.  We keep numbers as-is because '3pm' and '3 pm'
  sound different.
"""
from .cache import TTSCacheWrapper, TTSCache, normalize_key_text
from .warmup import warm_common_utterances

__all__ = [
    "TTSCacheWrapper",
    "TTSCache",
    "normalize_key_text",
    "warm_common_utterances",
]

"""Hash-keyed disk cache for TTS synth results.

Wraps a TTSProvider.  Every call to synthesize() / synthesize_from_plan()
routes through the cache.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Optional

from packages.runtime import telemetry as _tel


log = logging.getLogger(__name__)


# ── text normalisation ─────────────────────────────────────────────

_PUNCT_STRIP = re.compile(r"[.,!?;:]+\s*$")
_MULTISPACE = re.compile(r"\s+")


def normalize_key_text(text: str) -> str:
    """Cache-friendly normalisation.  Same phrase spoken with different
    casing/trailing-punctuation should hit the same entry.

    We do NOT normalise digits or units because '3pm' and '3 p.m.'
    actually synthesise to different audio."""
    if not text:
        return ""
    s = text.strip().lower()
    s = _PUNCT_STRIP.sub("", s)
    s = _MULTISPACE.sub(" ", s)
    return s


def _hash_key(voice: str, text: str, fmt: str, provider: str) -> str:
    """Stable content-addressed key.  Include provider so a
    Cartesia clip and an ElevenLabs clip of the same text don't
    collide."""
    material = f"{provider}|{voice}|{fmt}|{normalize_key_text(text)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


# ── on-disk layout ────────────────────────────────────────────────
# data/tts_cache/
#   entries/
#     <hash>.audio   # raw bytes returned by provider
#     <hash>.meta    # small JSON: {mime, provider, voice, text, format, ts, size}
#   index.jsonl      # append-only log used to rebuild in-memory index

_AUDIO_EXT = ".audio"
_META_EXT = ".meta"


@dataclass
class _Entry:
    key: str
    mime: str
    size: int
    last_access: float


class TTSCache:
    """Threadsafe (single-process) LRU disk cache.

    Not multi-process safe — a multi-worker deployment should either
    use separate cache dirs per worker or add a fcntl lock.  For a
    single uvicorn --workers 1 dev server this is fine.
    """

    def __init__(
        self,
        cache_dir: Optional[str] = None,
        max_bytes: int = 30 * 1024 * 1024,   # 30 MB
    ) -> None:
        base = cache_dir or os.environ.get("TTS_CACHE_DIR") or "data/tts_cache"
        self._base = os.path.abspath(base)
        self._entries_dir = os.path.join(self._base, "entries")
        os.makedirs(self._entries_dir, exist_ok=True)
        self._max_bytes = max_bytes
        self._lock = asyncio.Lock()
        self._index: dict[str, _Entry] = {}
        self._total_bytes = 0
        self._load_index()

    def _load_index(self) -> None:
        """Rebuild in-memory index from on-disk entries."""
        try:
            for fname in os.listdir(self._entries_dir):
                if not fname.endswith(_META_EXT):
                    continue
                key = fname[: -len(_META_EXT)]
                mpath = os.path.join(self._entries_dir, fname)
                apath = os.path.join(self._entries_dir, key + _AUDIO_EXT)
                if not os.path.exists(apath):
                    continue
                try:
                    with open(mpath, "r") as f:
                        meta = json.load(f)
                    size = int(meta.get("size", os.path.getsize(apath)))
                    self._index[key] = _Entry(
                        key=key,
                        mime=meta.get("mime", "application/octet-stream"),
                        size=size,
                        last_access=float(meta.get("ts", time.time())),
                    )
                    self._total_bytes += size
                except Exception as e:  # pragma: no cover
                    log.warning("tts_cache: skipping bad meta %s: %s", fname, e)
        except FileNotFoundError:
            pass
        log.info(
            "tts_cache loaded: %d entries, %.1f MB, cap %.1f MB, dir=%s",
            len(self._index), self._total_bytes / (1024 * 1024),
            self._max_bytes / (1024 * 1024), self._base,
        )
        _tel.TTS_CACHE_ENTRIES.set(len(self._index))
        _tel.TTS_CACHE_SIZE_BYTES.set(self._total_bytes)

    # ── public API ─────────────────────────────────────────────────

    def has(self, key: str) -> bool:
        return key in self._index

    async def get(self, key: str) -> Optional[tuple[bytes, str]]:
        """Return (audio, mime) or None on miss."""
        entry = self._index.get(key)
        if entry is None:
            return None
        apath = os.path.join(self._entries_dir, key + _AUDIO_EXT)
        try:
            with open(apath, "rb") as f:
                audio = f.read()
        except FileNotFoundError:
            # Index vs disk drift — self-heal.
            async with self._lock:
                self._index.pop(key, None)
                self._total_bytes -= entry.size
            return None
        entry.last_access = time.time()
        return audio, entry.mime

    async def put(
        self, key: str, audio: bytes, mime: str, text: str, voice: str,
        fmt: str, provider: str,
    ) -> None:
        """Store an entry.  Evicts LRU until under max_bytes."""
        if not audio:
            return
        size = len(audio)
        apath = os.path.join(self._entries_dir, key + _AUDIO_EXT)
        mpath = os.path.join(self._entries_dir, key + _META_EXT)
        try:
            with open(apath, "wb") as f:
                f.write(audio)
            with open(mpath, "w") as f:
                json.dump({
                    "mime": mime, "size": size,
                    "provider": provider, "voice": voice, "format": fmt,
                    "text": text[:200], "ts": time.time(),
                }, f)
        except Exception as e:
            log.warning("tts_cache put failed key=%s: %s", key, e)
            return

        async with self._lock:
            if key in self._index:
                # Replacement — drop old size before adding new.
                self._total_bytes -= self._index[key].size
            self._index[key] = _Entry(
                key=key, mime=mime, size=size, last_access=time.time(),
            )
            self._total_bytes += size
            _tel.TTS_CACHE_STORES.inc()
            await self._evict_if_needed()
            _tel.TTS_CACHE_ENTRIES.set(len(self._index))
            _tel.TTS_CACHE_SIZE_BYTES.set(self._total_bytes)

    async def _evict_if_needed(self) -> None:
        """Called under self._lock.  LRU-evict until under budget."""
        if self._total_bytes <= self._max_bytes:
            return
        by_lru = sorted(self._index.values(), key=lambda e: e.last_access)
        for e in by_lru:
            if self._total_bytes <= self._max_bytes:
                break
            try:
                os.remove(os.path.join(self._entries_dir, e.key + _AUDIO_EXT))
                os.remove(os.path.join(self._entries_dir, e.key + _META_EXT))
            except FileNotFoundError:
                pass
            self._total_bytes -= e.size
            self._index.pop(e.key, None)
            log.info("tts_cache evicted key=%s size=%d", e.key, e.size)


# ── wrapper ────────────────────────────────────────────────────────


class TTSCacheWrapper:
    """Wraps a TTSProvider.  Transparent — same interface, cached
    responses.

    Cache key derived from (provider name, voice binding, output
    format, normalised text).  Miss path calls the inner provider,
    stores the result, returns it.

    Errors from the inner provider propagate unchanged — cache
    NEVER masks a provider failure with stale audio.
    """

    def __init__(self, inner, cache: Optional[TTSCache] = None) -> None:
        self._inner = inner
        self._cache = cache or _get_shared_cache()
        # Expose inner provider attributes so the wrapper is fully
        # substitutable (name, default_voice, output_format, etc).
        self.name = getattr(inner, "name", "tts") + "+cache"

    def __getattr__(self, item: str) -> Any:
        # Anything the wrapper doesn't override falls through to the
        # inner provider (default_voice, mime, output_format, model, ...)
        return getattr(self._inner, item)

    def _voice_for(self, voice: Optional[str]) -> str:
        return voice or getattr(self._inner, "default_voice", "default")

    def _format_for(self) -> str:
        return getattr(self._inner, "output_format", "unknown")

    async def synthesize(self, text: str, voice: Optional[str] = None) -> tuple[bytes, str]:
        provider = getattr(self._inner, "name", "tts")
        v = self._voice_for(voice)
        fmt = self._format_for()
        key = _hash_key(v, text, fmt, provider)

        t0 = time.perf_counter()
        try:
            hit = await self._cache.get(key)
        except Exception as e:
            log.warning("tts_cache lookup failed: %s (falling through to provider)", e)
            _tel.TTS_CACHE_LOOKUPS.labels(result="error").inc()
            hit = None

        if hit is not None:
            audio, mime = hit
            _tel.TTS_CACHE_LOOKUPS.labels(result="hit").inc()
            _tel.TTS_CACHE_HIT_LATENCY.observe(time.perf_counter() - t0)
            log.info(
                "tts_cache HIT provider=%s voice=%s fmt=%s bytes=%d text=%r",
                provider, v, fmt, len(audio), text[:60],
            )
            return audio, mime

        # Miss → provider call.
        _tel.TTS_CACHE_LOOKUPS.labels(result="miss").inc()
        audio, mime = await self._inner.synthesize(text, voice=voice)
        # Fire-and-forget the disk write so we don't add write latency
        # to the caller's path.  Use asyncio.shield so cancellation of
        # the caller doesn't kill the write mid-fsync.
        put_coro = self._cache.put(
            key=key, audio=audio, mime=mime, text=text,
            voice=v, fmt=fmt, provider=provider,
        )
        async def _write():
            try:
                await asyncio.shield(put_coro)
            except Exception as e:
                log.warning("tts_cache background put failed: %s", e)
        asyncio.create_task(_write(), name=f"tts-cache-write-{key[:8]}")
        _tel.TTS_CACHE_MISS_LATENCY.observe(time.perf_counter() - t0)
        log.info(
            "tts_cache MISS provider=%s voice=%s fmt=%s bytes=%d text=%r",
            provider, v, fmt, len(audio), text[:60],
        )
        return audio, mime

    async def synthesize_from_plan(self, plan, voice: Optional[str] = None) -> tuple[bytes, str]:
        """VPL path.  Cache key includes the compiled request payload
        hash so different deliveries (fast/slow/warm/cold) of the same
        text cache separately."""
        provider = getattr(self._inner, "name", "tts")
        v = self._voice_for(voice)
        fmt = getattr(plan, "output_format", self._format_for())
        # Include the payload signature so different VPL deliveries
        # don't collide (fast vs warm vs slow are distinct audio).
        payload_sig = ""
        try:
            payload = getattr(plan, "request_payload", {})
            payload_sig = json.dumps(payload, sort_keys=True)[:2000]
        except Exception:
            pass
        text = ""
        try:
            text = plan.request_payload.get("text", "") if hasattr(plan, "request_payload") else ""
        except Exception:
            pass
        key_material = payload_sig + "|" + text
        key = _hash_key(v, key_material, fmt, provider + ":vpl")

        t0 = time.perf_counter()
        hit = await self._cache.get(key)
        if hit is not None:
            audio, mime = hit
            _tel.TTS_CACHE_LOOKUPS.labels(result="hit").inc()
            _tel.TTS_CACHE_HIT_LATENCY.observe(time.perf_counter() - t0)
            log.info(
                "tts_cache HIT (vpl) provider=%s voice=%s fmt=%s bytes=%d",
                provider, v, fmt, len(audio),
            )
            return audio, mime

        _tel.TTS_CACHE_LOOKUPS.labels(result="miss").inc()
        audio, mime = await self._inner.synthesize_from_plan(plan, voice=voice)
        asyncio.create_task(
            asyncio.shield(self._cache.put(
                key=key, audio=audio, mime=mime, text=text[:200] or "(vpl)",
                voice=v, fmt=fmt, provider=provider + ":vpl",
            )),
            name=f"tts-cache-write-vpl-{key[:8]}",
        )
        _tel.TTS_CACHE_MISS_LATENCY.observe(time.perf_counter() - t0)
        log.info(
            "tts_cache MISS (vpl) provider=%s voice=%s fmt=%s bytes=%d",
            provider, v, fmt, len(audio),
        )
        return audio, mime


# ── module-level singleton so all wrappers share one cache ────────

_SHARED_CACHE: Optional[TTSCache] = None


def _get_shared_cache() -> TTSCache:
    global _SHARED_CACHE
    if _SHARED_CACHE is None:
        _SHARED_CACHE = TTSCache()
    return _SHARED_CACHE


def get_shared_cache() -> TTSCache:
    """Public accessor for tests and warmup."""
    return _get_shared_cache()

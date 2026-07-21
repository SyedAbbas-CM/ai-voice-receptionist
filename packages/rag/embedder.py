"""Embedder abstraction — text to vector.

Swap between local (BGE, MiniLM) and cloud (OpenAI, Cohere) via
the build_embedder() factory. Same interface for every backend so
retrievers don't care what produced their vectors.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional


class Embedder(ABC):
    """Text -> vector."""

    name: str = "base"
    dim: int = 0                # embedding dimension, set by subclass

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Batch embed. Returns one vector per input text."""

    async def embed_one(self, text: str) -> list[float]:
        result = await self.embed([text])
        return result[0]


class NoopEmbedder(Embedder):
    """Stub for tests. Returns zero-vectors of a fixed dim."""

    name = "noop"

    def __init__(self, dim: int = 8):
        self.dim = dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * self.dim for _ in texts]


class LocalBGEEmbedder(Embedder):
    """BGE-small-en-v1.5 (~33M params, 384-dim) via sentence-transformers.

    MPS-friendly on M1 Pro. Runs at ~50 embeddings/sec locally.
    Free forever, no API dependency."""

    name = "local"
    dim = 384

    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5", device: str = "auto"):
        self.model_name = model_name
        self._device_setting = device
        self._model = None

    def _resolve_device(self) -> str:
        if self._device_setting != "auto":
            return self._device_setting
        try:
            import torch
            if torch.cuda.is_available():
                return "cuda"
            if torch.backends.mps.is_available():
                return "mps"
        except ImportError:
            pass
        return "cpu"

    def _load(self):
        if self._model is not None:
            return
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise RuntimeError(
                "LocalBGEEmbedder needs `pip install sentence-transformers`."
            ) from e
        self._model = SentenceTransformer(self.model_name, device=self._resolve_device())

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        self._load()
        # BGE recommends this prefix for query encoding but not for docs.
        # Since we don't know if it's a query or a doc here, skip the prefix
        # and let the retriever add it at query time.
        vectors = self._model.encode(texts, convert_to_numpy=True, normalize_embeddings=True)
        return [v.tolist() for v in vectors]


class OpenAIEmbedder(Embedder):
    """OpenAI text-embedding-3-small (1536-dim) or -large (3072-dim).

    ~$0.02 / 1M tokens for small; sub-100ms per batch of 20."""

    name = "openai"

    def __init__(self, model: str = "text-embedding-3-small", api_key: Optional[str] = None):
        self.model = model
        self.api_key = api_key
        self.dim = 1536 if "small" in model else 3072

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if not self.api_key:
            raise RuntimeError("OpenAIEmbedder needs api_key (or OPENAI_API_KEY env)")
        import httpx
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.openai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": self.model, "input": texts},
            )
            resp.raise_for_status()
            data = resp.json()
        return [item["embedding"] for item in data["data"]]


def build_embedder(kind: str = "local", **kwargs) -> Embedder:
    kind = (kind or "local").lower()
    if kind == "local":
        return LocalBGEEmbedder(**kwargs)
    if kind == "openai":
        return OpenAIEmbedder(**kwargs)
    if kind == "noop":
        return NoopEmbedder(**kwargs)
    raise ValueError(f"unknown embedder kind: {kind!r}")

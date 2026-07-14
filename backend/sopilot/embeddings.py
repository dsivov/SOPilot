"""Embedding provider abstraction. Vectors are unit-normalized float32 so cosine
similarity is a plain dot product everywhere downstream (rerank, pgvector).
"""
from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod

import numpy as np

from .config import get_settings


class EmbeddingProvider(ABC):
    dim: int

    @abstractmethod
    async def embed(self, text: str) -> np.ndarray: ...

    async def embed_many(self, texts: list[str]) -> list[np.ndarray]:
        return [await self.embed(t) for t in texts]


def _normalize(v: np.ndarray) -> np.ndarray:
    v = v.astype(np.float32)
    n = float(np.linalg.norm(v))
    return v / n if n else v


class OpenAIEmbeddings(EmbeddingProvider):
    def __init__(self, model: str | None = None):
        settings = get_settings()
        self.model = model or settings.embedding_model
        self.dim = settings.embedding_dim
        self._client = None

    def _get_client(self):
        if self._client is None:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI()
        return self._client

    async def embed(self, text: str) -> np.ndarray:
        res = await self._get_client().embeddings.create(model=self.model, input=text)
        return _normalize(np.array(res.data[0].embedding, dtype=np.float32))

    async def embed_many(self, texts: list[str]) -> list[np.ndarray]:
        if not texts:
            return []
        res = await self._get_client().embeddings.create(model=self.model, input=texts)
        return [_normalize(np.array(d.embedding, dtype=np.float32)) for d in res.data]


class FakeEmbeddings(EmbeddingProvider):
    """Deterministic hash-seeded vectors for tests — no network, stable across runs.

    Identical strings map to identical vectors; distinct strings are (almost surely)
    non-parallel, so cosine ordering is stable and duplicates hit the dedup threshold.
    """

    def __init__(self, dim: int = 32):
        self.dim = dim

    async def embed(self, text: str) -> np.ndarray:
        seed = int.from_bytes(hashlib.sha256(text.strip().lower().encode()).digest()[:8], "big")
        rng = np.random.default_rng(seed)
        return _normalize(rng.standard_normal(self.dim))


def cosine(a: np.ndarray | None, b: np.ndarray | None) -> float:
    if a is None or b is None or a.size == 0 or b.size == 0 or a.shape != b.shape:
        return 0.0
    return float(np.dot(a, b))  # inputs are unit-normalized


def pack_embedding(v: np.ndarray | None) -> bytes:
    return v.astype(np.float32).tobytes() if v is not None and v.size else b""


def unpack_embedding(b: bytes) -> np.ndarray | None:
    return np.frombuffer(b, dtype=np.float32) if b else None

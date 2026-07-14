"""Redis-backed session pool — the blackboard every live agent reads.

Replaces the POC's process-memory singleton so the runtime is multi-worker and
horizontally scalable. Semantics preserved from the validated design:
  - misprediction-tolerant: items sit in the pool until TTL, whoever fetched them;
  - 30-item cap, lowest-confidence evicted first;
  - summary embeddings precomputed at insert so the per-turn rerank pays no
    embedding cost for pool items.

Layout: one Redis hash per session, field = item_id, value = JSON-encoded item.
The hash key expires session_ttl_s after the last write.
"""
from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import numpy as np
import redis.asyncio as aioredis

from .config import get_settings
from .embeddings import pack_embedding, unpack_embedding
from .tenancy import Scope


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class PoolItem:
    dependency_name: str
    source_action: str
    payload: object
    payload_summary: str
    confidence: float
    fetched_at: datetime = field(default_factory=utcnow)
    expires_at: datetime = field(default_factory=lambda: utcnow() + timedelta(seconds=300))
    item_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    fetch_id: str = ""  # data_fetches audit row id
    predictor_source: str = "empirical"  # empirical | live | pondering | generative
    source_query: str | None = None
    predicted_user_state: str | None = None
    kind: str = "data"  # data | instruction
    instr_data_count: int = 0
    summary_embedding: np.ndarray | None = None

    def expired(self, now: datetime | None = None) -> bool:
        return (now or utcnow()) >= self.expires_at

    def to_json(self) -> str:
        return json.dumps(
            {
                "item_id": self.item_id,
                "fetch_id": self.fetch_id,
                "fetched_at": self.fetched_at.isoformat(),
                "expires_at": self.expires_at.isoformat(),
                "dependency_name": self.dependency_name,
                "source_action": self.source_action,
                "payload": self.payload,
                "payload_summary": self.payload_summary,
                "confidence": self.confidence,
                "predictor_source": self.predictor_source,
                "source_query": self.source_query,
                "predicted_user_state": self.predicted_user_state,
                "kind": self.kind,
                "instr_data_count": self.instr_data_count,
                "summary_embedding": base64.b64encode(pack_embedding(self.summary_embedding)).decode(),
            }
        )

    @classmethod
    def from_json(cls, raw: str | bytes) -> "PoolItem":
        d = json.loads(raw)
        return cls(
            item_id=d["item_id"],
            fetch_id=d.get("fetch_id", ""),
            fetched_at=datetime.fromisoformat(d["fetched_at"]),
            expires_at=datetime.fromisoformat(d["expires_at"]),
            dependency_name=d["dependency_name"],
            source_action=d.get("source_action", ""),
            payload=d.get("payload"),
            payload_summary=d.get("payload_summary", ""),
            confidence=float(d.get("confidence", 0.0)),
            predictor_source=d.get("predictor_source", "empirical"),
            source_query=d.get("source_query"),
            predicted_user_state=d.get("predicted_user_state"),
            kind=d.get("kind", "data"),
            instr_data_count=int(d.get("instr_data_count", 0)),
            summary_embedding=unpack_embedding(base64.b64decode(d.get("summary_embedding", ""))),
        )


class SessionPool:
    """All operations are scoped: the Redis key embeds tenant + project."""

    def __init__(self, redis: aioredis.Redis, *, max_items: int | None = None, session_ttl_s: int | None = None):
        settings = get_settings()
        self.redis = redis
        self.max_items = max_items or settings.pool_max_items
        self.session_ttl_s = session_ttl_s or settings.session_ttl_s

    def _key(self, scope: Scope, session_id: str) -> str:
        return f"{scope.redis_prefix()}:pool:{session_id}"

    async def insert(self, scope: Scope, session_id: str, item: PoolItem) -> None:
        key = self._key(scope, session_id)
        await self.redis.hset(key, item.item_id, item.to_json())
        await self.redis.expire(key, self.session_ttl_s)
        await self._prune(key)

    async def _prune(self, key: str) -> None:
        """Drop expired items; enforce the cap by evicting lowest-confidence."""
        raw = await self.redis.hgetall(key)
        if not raw:
            return
        now = utcnow()
        items: list[PoolItem] = []
        stale: list[str | bytes] = []
        for field_name, val in raw.items():
            it = PoolItem.from_json(val)
            if it.expired(now):
                stale.append(field_name)
            else:
                items.append(it)
        if stale:
            await self.redis.hdel(key, *stale)
        if len(items) > self.max_items:
            items.sort(key=lambda p: p.confidence, reverse=True)
            victims = [p.item_id for p in items[self.max_items :]]
            if victims:
                await self.redis.hdel(key, *victims)

    async def get_pool(self, scope: Scope, session_id: str) -> list[PoolItem]:
        """Live (non-expired) items, recency-desc — the order the rerank expects."""
        raw = await self.redis.hgetall(self._key(scope, session_id))
        now = utcnow()
        items = [PoolItem.from_json(v) for v in raw.values()]
        live = [p for p in items if not p.expired(now)]
        live.sort(key=lambda p: p.fetched_at, reverse=True)
        return live

    async def lookup_instruction(
        self, scope: Scope, session_id: str, *, chosen_action: str, classified_state: str
    ) -> PoolItem | None:
        """Exact-match instruction lookup (Milestone B v0 semantics from the POC)."""
        if not chosen_action or not classified_state:
            return None
        for p in await self.get_pool(scope, session_id):
            if (
                p.kind == "instruction"
                and p.source_action == chosen_action
                and p.predicted_user_state == classified_state
            ):
                return p
        return None

    async def clear(self, scope: Scope, session_id: str) -> None:
        await self.redis.delete(self._key(scope, session_id))

    # ----- in-flight dedup markers (cross-worker) -----

    def _inflight_key(self, scope: Scope, session_id: str, fetch_key: str) -> str:
        return f"{scope.redis_prefix()}:inflight:{session_id}:{fetch_key}"

    async def try_claim_fetch(self, scope: Scope, session_id: str, fetch_key: str, ttl_s: int = 120) -> bool:
        """SET NX marker so two workers don't fire the same speculative fetch."""
        return bool(
            await self.redis.set(self._inflight_key(scope, session_id, fetch_key), "1", nx=True, ex=ttl_s)
        )

    async def release_fetch(self, scope: Scope, session_id: str, fetch_key: str) -> None:
        await self.redis.delete(self._inflight_key(scope, session_id, fetch_key))

    async def is_inflight(self, scope: Scope, session_id: str, fetch_key: str) -> bool:
        return bool(await self.redis.exists(self._inflight_key(scope, session_id, fetch_key)))

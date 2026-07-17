"""Prefetch manager — schedules speculative fetches from a plan and serves the
consume path. Stateless across workers: pool contents and in-flight dedup markers
live in Redis; every fetch lifecycle is audited to Postgres.

Invariants carried over from the research:
  - only idempotent dependencies fire speculatively (enforced here);
  - a completed fetch always lands in the pool, whoever predicted it —
    mispredictions stay reusable candidates (the whole point of the pool);
  - audit rows never break a fetch (best-effort, logged).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from datetime import timedelta

from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker

from .embeddings import EmbeddingProvider
from .fetchers.base import get_fetcher
from .config import get_settings
from .models import DataFetchAudit
from .pool import PoolItem, SessionPool, utcnow
from .predictor import PrefetchPlanItem
from .schemas import TaskDefinition
from .tenancy import Scope

log = logging.getLogger(__name__)


def fetch_key(dep_name: str, action_name: str, query_hash: str = "") -> str:
    return hashlib.sha1(f"{dep_name}|{action_name}|{query_hash}".encode()).hexdigest()[:24]


def payload_prompt_text(payload: object, summary: str) -> str:
    """What the AGENT should see for a consumed dependency. Rich fetchers (RAG,
    MCP, HTTP) carry their content in the payload; the summary is a ≤200-char
    meta-line meant for rerank/audit, not for answering. Prefer content."""
    if isinstance(payload, dict):
        for key in ("joined_text", "text", "content", "result"):
            v = payload.get(key)
            if isinstance(v, str) and v.strip():
                return v[:3000]
    if isinstance(payload, str) and payload.strip():
        return payload[:3000]
    if payload is not None and not isinstance(payload, (dict, list)):
        return str(payload)[:400]
    return summary or (str(payload)[:400] if payload is not None else "")


def query_hash(query: str | None) -> str:
    if not query:
        return ""
    return hashlib.sha1(query.strip().lower().encode()).hexdigest()[:12]


class PrefetchManager:
    def __init__(
        self,
        pool: SessionPool,
        sessionmaker: async_sessionmaker,
        embedder: EmbeddingProvider,
    ):
        self.pool = pool
        self.sessionmaker = sessionmaker
        self.embedder = embedder
        self._tasks: set[asyncio.Task] = set()

    # ----- scheduling -----

    async def schedule(
        self,
        *,
        scope: Scope,
        session_id: str,
        task_def: TaskDefinition,
        plan: list[PrefetchPlanItem],
        current_turn_index: int,
        min_confidence: float = 0.05,
    ) -> int:
        """Launch background fetches for plan items above the confidence floor.
        Returns how many were launched (dedup + idempotency filtered)."""
        dep_by_name = {d.name: d for d in task_def.data_dependencies}
        launched = 0
        for item in plan:
            if item.confidence < min_confidence:
                continue
            dep = dep_by_name.get(item.dependency_name)
            if dep is None or not dep.idempotent:
                continue  # non-idempotent deps NEVER fire speculatively
            qh = query_hash(item.rendered_query)
            key = fetch_key(dep.name, item.action_name, qh)
            if not await self.pool.try_claim_fetch(scope, session_id, key, ttl_s=max(30, dep.cache_ttl_s)):
                continue  # already in flight or recently claimed (any worker)
            task = asyncio.create_task(
                self._run_fetch(
                    scope=scope,
                    session_id=session_id,
                    dep=dep,
                    key=key,
                    action_name=item.action_name,
                    confidence=item.confidence,
                    issued_at_turn=current_turn_index,
                    predicted_turn=current_turn_index + item.predicted_turn_offset,
                    speculative=True,
                    predictor_source=item.predictor_source,
                    predicted_user_state=item.predicted_user_state,
                    rendered_query=item.rendered_query,
                    qh=qh,
                )
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            launched += 1
        return launched

    async def _run_fetch(
        self,
        *,
        scope: Scope,
        session_id: str,
        dep,
        key: str,
        action_name: str,
        confidence: float,
        issued_at_turn: int,
        predicted_turn: int,
        speculative: bool,
        predictor_source: str,
        predicted_user_state: str | None = None,
        rendered_query: str | None = None,
        qh: str = "",
    ) -> PoolItem | None:
        started_at = utcnow()
        t0 = time.perf_counter()
        payload = None
        summary = ""
        err: str | None = None
        connector_name = ""
        try:
            from .connectors import resolve_dependency

            dep, connector_name = await resolve_dependency(self.sessionmaker, scope, dep)
            fetcher = get_fetcher(dep.kind)
            outcome = await fetcher.fetch(
                dep, scope=scope, session_id=session_id, action_name=action_name, query=rendered_query
            )
            payload, summary = outcome.payload, outcome.summary
        except Exception as e:  # noqa: BLE001 — a failed fetch must not kill the lane
            err = f"{type(e).__name__}: {e}"
        # NOTE: the in-flight claim is released AFTER the pool insert (see the
        # tail of this function) — releasing here made consume() wake up,
        # find the pool still empty, and fire a redundant live fetch.
        duration_ms = int((time.perf_counter() - t0) * 1000)
        completed_at = utcnow()
        log.info(
            "fetch %s dep=%s kind=%s connector=%s ms=%d speculative=%s source=%s%s",
            "FAILED" if err else "ok", dep.name, dep.kind, connector_name or "-",
            duration_ms, speculative, predictor_source, f" err={err}" if err else "",
        )

        fetch_id = ""
        try:
            async with self.sessionmaker() as db:
                # Late-landing fetch on an already-ended session (the background
                # lane has no deadline): record it as wasted immediately — the
                # /end finalizer has already swept and won't come back for it.
                from sqlalchemy import select as _select

                from .models import ConversationSession

                sess_status = (
                    await db.execute(
                        _select(ConversationSession.status).where(ConversationSession.id == session_id)
                    )
                ).scalar_one_or_none()
                row = DataFetchAudit(
                    tenant_id=scope.tenant_id,
                    project_id=scope.project_id,
                    session_id=session_id,
                    dependency_name=dep.name,
                    action_name=action_name,
                    kind=dep.kind,
                    connector=connector_name,
                    speculative=speculative,
                    predictor_source=predictor_source,
                    confidence=confidence,
                    issued_at_turn=issued_at_turn,
                    predicted_turn=predicted_turn,
                    query_text=rendered_query,
                    query_hash=qh or None,
                    started_at=started_at,
                    completed_at=completed_at,
                    fetch_duration_ms=duration_ms,
                    payload_summary=(summary or "")[:500],
                    fetch_error=err,
                    wasted=(sess_status == "ended" and speculative),
                )
                db.add(row)
                await db.commit()
                fetch_id = row.id
        except Exception:
            log.exception("data_fetches audit write failed (fetch continues)")

        if err is not None or payload is None:
            if speculative:
                await self.pool.release_fetch(scope, session_id, key)
            return None

        summary_emb = None
        # For query-driven fetches embed the RENDERED QUERY, not the payload
        # header: rich fetchers (CG/RAG) prefix every response with identical
        # boilerplate, which made content embeddings useless for both rerank
        # and the D-12 staleness gate (query-vs-query is the right comparison).
        short_summary = (summary or "")[:200]
        emb_text = (rendered_query or "").strip() or short_summary
        if emb_text:
            try:
                summary_emb = await self.embedder.embed(emb_text[:300])
            except Exception:
                summary_emb = None
        item = PoolItem(
            fetch_id=fetch_id,
            fetched_at=completed_at,
            expires_at=completed_at + timedelta(seconds=dep.cache_ttl_s),
            dependency_name=dep.name,
            source_action=action_name,
            payload=payload,
            payload_summary=short_summary,
            confidence=confidence,
            predictor_source=predictor_source,
            source_query=rendered_query,
            predicted_user_state=predicted_user_state,
            summary_embedding=summary_emb,
        )
        await self.pool.insert(scope, session_id, item)
        if speculative:
            await self.pool.release_fetch(scope, session_id, key)
        return item

    # ----- consumption -----

    def prefetch_current_turn(
        self, *, scope: Scope, session_id: str, task_def: TaskDefinition,
        user_text: str, cohort: str = "", mood: str = "", state: str = "",
    ) -> None:
        """Fire-and-forget: fetch every {user_text}-templated idempotent dep
        with the CURRENT utterance, in parallel with classification. Turn 0
        (nothing prefetched yet) goes from serial classify→fetch to
        max(classify, fetch); consume() awaits these via the turnfetch key."""
        for dep in task_def.data_dependencies:
            if not dep.idempotent or not dep.query_template or "{user_text}" not in dep.query_template:
                continue
            try:
                rendered = dep.query_template.format(
                    user_text=user_text, cohort=cohort, mood=mood, state=state, action=""
                )
            except (KeyError, IndexError):
                continue
            key = f"turnfetch:{dep.name}"

            async def _go(dep=dep, key=key, rendered=rendered) -> None:
                if not await self.pool.try_claim_fetch(scope, session_id, key, ttl_s=60):
                    return  # already in flight for this turn
                await self._run_fetch(
                    scope=scope, session_id=session_id, dep=dep, key=key,
                    action_name="__current_turn", confidence=1.0,
                    issued_at_turn=0, predicted_turn=0, speculative=True,
                    predictor_source="turn", rendered_query=rendered,
                    qh=query_hash(rendered),
                )

            asyncio.ensure_future(_go())

    async def consume(
        self,
        *,
        scope: Scope,
        session_id: str,
        task_def: TaskDefinition,
        action_name: str,
        current_turn_index: int,
        await_inflight_ms: int = 6000,
        live_fallback: bool = True,
        user_text: str = "",
        cohort: str = "",
        mood: str = "",
        state: str = "",
        query_emb=None,
    ) -> tuple[dict[str, str], dict[str, int]]:
        """Resolve the chosen action's declared deps from the pool; poll briefly for
        in-flight fetches; optionally live-fetch misses (audited as such)."""
        action_obj = next((a for a in task_def.agent_actions if a.name == action_name), None)
        stats = {"consumed": 0, "live": 0, "latency_hidden_ms": 0, "live_latency_ms": 0}
        if action_obj is None or not action_obj.data_dependencies:
            return {}, stats
        dep_by_name = {d.name: d for d in task_def.data_dependencies}
        payloads: dict[str, str] = {}

        for dep_name in action_obj.data_dependencies:
            dep = dep_by_name.get(dep_name)
            if dep is None:
                continue
            item = await self._pool_lookup(scope, session_id, dep_name)
            if item is None and await_inflight_ms > 0:
                keys = (fetch_key(dep_name, action_name), f"turnfetch:{dep_name}")
                deadline = time.perf_counter() + await_inflight_ms / 1000.0
                while time.perf_counter() < deadline:
                    inflight = False
                    for k in keys:
                        if await self.pool.is_inflight(scope, session_id, k):
                            inflight = True
                            break
                    if not inflight:
                        item = await self._pool_lookup(scope, session_id, dep_name)
                        break
                    await asyncio.sleep(0.1)
                else:
                    item = await self._pool_lookup(scope, session_id, dep_name)
            if item is not None:
                # D-12: staleness gate for user-text-driven deps — a speculatively
                # fetched item answers the PREDICTED utterance; if its content is
                # semantically far from what the caller ACTUALLY just said, re-fetch
                # live with the real query instead of serving stale context.
                stale = False
                min_cos = get_settings().consume_stale_min_cos
                if (
                    min_cos > 0
                    and live_fallback
                    and query_emb is not None
                    and item.summary_embedding is not None
                    and dep.query_template
                    and "{user_text}" in dep.query_template
                ):
                    import numpy as _np

                    a = _np.asarray(query_emb, dtype=float)
                    b = _np.asarray(item.summary_embedding, dtype=float)
                    denom = float(_np.linalg.norm(a) * _np.linalg.norm(b)) or 1.0
                    cos = float(a.dot(b)) / denom
                    if cos < min_cos:
                        stale = True
                        log.info(
                            "stale speculation dep=%s cos=%.3f < %.2f — live re-fetch with real query",
                            dep_name, cos, min_cos,
                        )
                if not stale:
                    stats["consumed"] += 1
                    payloads[dep_name] = payload_prompt_text(item.payload, item.payload_summary)
                    await self._mark_consumed(scope, item.fetch_id, current_turn_index)
                    continue
            if live_fallback:
                # live fallback renders the SAME query template the speculative
                # path would — with the actual user text, which is strictly
                # better context than a prediction-time synthesis.
                rendered_query = None
                if dep.query_template:
                    try:
                        rendered_query = dep.query_template.format(
                            user_text=user_text, cohort=cohort, mood=mood,
                            state=state, action=action_name,
                        )
                    except (KeyError, IndexError):
                        rendered_query = None
                t0 = time.perf_counter()
                live_item = await self._run_fetch(
                    scope=scope,
                    session_id=session_id,
                    dep=dep,
                    key=fetch_key(dep_name, action_name),
                    action_name=action_name,
                    confidence=0.0,
                    issued_at_turn=current_turn_index,
                    predicted_turn=current_turn_index,
                    speculative=False,
                    predictor_source="live",
                    rendered_query=rendered_query,
                    qh=query_hash(rendered_query) if rendered_query else "",
                )
                stats["live"] += 1
                stats["live_latency_ms"] += int((time.perf_counter() - t0) * 1000)
                if live_item is not None:
                    payloads[dep_name] = payload_prompt_text(live_item.payload, live_item.payload_summary)

        # latency_hidden = what the consumed fetches originally cost off-path
        if stats["consumed"]:
            stats["latency_hidden_ms"] = await self._sum_hidden_latency(scope, session_id, payloads.keys())
        return payloads, stats

    async def _pool_lookup(self, scope: Scope, session_id: str, dep_name: str) -> PoolItem | None:
        """Freshest live pool item for a dependency (pool is recency-desc)."""
        for p in await self.pool.get_pool(scope, session_id):
            if p.kind == "data" and p.dependency_name == dep_name:
                return p
        return None

    async def _mark_consumed(self, scope: Scope, fetch_id: str, turn_index: int) -> None:
        if not fetch_id:
            return
        try:
            async with self.sessionmaker() as db:
                await db.execute(
                    update(DataFetchAudit)
                    .where(
                        DataFetchAudit.id == fetch_id,
                        DataFetchAudit.tenant_id == scope.tenant_id,
                        DataFetchAudit.consumed.is_(False),
                    )
                    .values(consumed=True, consumed_at_turn=turn_index)
                )
                await db.commit()
        except Exception:
            log.exception("mark-consumed audit write failed")

    async def _sum_hidden_latency(self, scope: Scope, session_id: str, dep_names) -> int:
        try:
            from sqlalchemy import func, select

            async with self.sessionmaker() as db:
                total = (
                    await db.execute(
                        select(func.coalesce(func.sum(DataFetchAudit.fetch_duration_ms), 0)).where(
                            DataFetchAudit.tenant_id == scope.tenant_id,
                            DataFetchAudit.session_id == session_id,
                            DataFetchAudit.dependency_name.in_(list(dep_names)),
                            DataFetchAudit.speculative.is_(True),
                            DataFetchAudit.consumed.is_(True),
                        )
                    )
                ).scalar_one()
                return int(total or 0)
        except Exception:
            return 0

    # ----- session lifecycle -----

    async def finalize_session(self, scope: Scope, session_id: str) -> None:
        """Mark unconsumed speculative fetches wasted; drop the pool."""
        try:
            async with self.sessionmaker() as db:
                await db.execute(
                    update(DataFetchAudit)
                    .where(
                        DataFetchAudit.tenant_id == scope.tenant_id,
                        DataFetchAudit.session_id == session_id,
                        DataFetchAudit.consumed.is_(False),
                        DataFetchAudit.wasted.is_(False),
                    )
                    .values(wasted=True)
                )
                await db.commit()
        except Exception:
            log.exception("finalize_session audit write failed")
        await self.pool.clear(scope, session_id)

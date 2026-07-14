"""sopilot-supervisor — the background lane (D-1).

A consumer-group worker over the turn-event stream. Per event, when the
project's mode enables predicted retrieval (D-9): run the precedent predictor,
build the prefetch plan, launch speculative fetches into the session pool, ack.
Crashes are safe: unacked events are reclaimed via XAUTOCLAIM by any worker.

Runs standalone (`sopilot-supervisor`, N replicas) or embedded in the API
process behind SOPILOT_EMBEDDED_SUPERVISOR=true (dev). Same code path.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal

import redis.asyncio as aioredis
from sqlalchemy import select

from .config import get_settings
from .db import get_sessionmaker
from .embeddings import EmbeddingProvider, OpenAIEmbeddings
from .events import TURN_STREAM, TurnEvent, ensure_group
from .models import SopVersion
from .pool import SessionPool
from .predictor import EmpiricalTrajectoryPredictor, build_prefetch_plan
from .prefetch import PrefetchManager
from .schemas import TaskDefinition

log = logging.getLogger(__name__)


class SupervisorWorker:
    def __init__(
        self,
        redis: aioredis.Redis,
        *,
        embedder: EmbeddingProvider | None = None,
        consumer_name: str | None = None,
    ):
        settings = get_settings()
        self.redis = redis
        self.group = settings.supervisor_group
        self.consumer = consumer_name or f"sup-{os.getpid()}"
        self.block_ms = settings.supervisor_block_ms
        self.batch = settings.supervisor_batch
        self.autoclaim_idle_ms = settings.supervisor_autoclaim_idle_ms
        self.embedder = embedder or OpenAIEmbeddings()
        self.pool = SessionPool(redis)
        self.prefetch = PrefetchManager(self.pool, get_sessionmaker(), self.embedder)
        self._stop = asyncio.Event()
        self._sop_cache: dict[tuple[str, int], TaskDefinition] = {}

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        await ensure_group(self.redis, self.group)
        log.info("supervisor %s consuming %s (group=%s)", self.consumer, TURN_STREAM, self.group)
        autoclaim_cursor = "0-0"
        while not self._stop.is_set():
            try:
                # 1) reclaim stale pending entries from dead workers
                try:
                    reply = await self.redis.xautoclaim(
                        TURN_STREAM, self.group, self.consumer,
                        min_idle_time=self.autoclaim_idle_ms, start_id=autoclaim_cursor, count=self.batch,
                    )
                    autoclaim_cursor = reply[0] if reply else "0-0"
                    claimed = reply[1] if reply and len(reply) > 1 else []
                    for entry_id, fields in claimed:
                        await self._handle(entry_id, fields)
                except Exception:
                    log.exception("xautoclaim sweep failed (continuing)")

                # 2) fresh events
                resp = await self.redis.xreadgroup(
                    self.group, self.consumer, {TURN_STREAM: ">"}, count=self.batch, block=self.block_ms
                )
                for _stream, entries in resp or []:
                    for entry_id, fields in entries:
                        await self._handle(entry_id, fields)
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("supervisor loop error; backing off 1s")
                await asyncio.sleep(1.0)
        # drain in-flight fetch tasks before exiting
        if self.prefetch._tasks:
            await asyncio.gather(*self.prefetch._tasks, return_exceptions=True)

    async def _handle(self, entry_id, fields) -> None:
        """Process one turn event. Always ack — an event that fails processing is
        logged and dropped rather than poisoning the group (prefetch is best-effort
        by design; the online lane's live fallback covers any gap)."""
        try:
            event = TurnEvent.from_fields(fields)
            scope = event.scope()
            if scope.retrieval_enabled and event.action:
                await self._predict_and_prefetch(event)
        except Exception:
            log.exception("turn event %s failed; dropped (live fallback covers it)", entry_id)
        finally:
            try:
                await self.redis.xack(TURN_STREAM, self.group, entry_id)
            except Exception:
                log.exception("xack failed for %s", entry_id)

    async def _predict_and_prefetch(self, event: TurnEvent) -> None:
        task_def = await self._load_sop(event)
        if task_def is None:
            return
        scope = event.scope()
        async with get_sessionmaker()() as db:
            predictor = EmpiricalTrajectoryPredictor(
                db, scope,
                sop_id=event.sop_id,
                cohort=event.cohort,
                chosen_action=event.action,
                mood=event.mood or None,
            )
            predictions = await predictor.predict(max_offset=3)
        if not predictions:
            return
        plan = build_prefetch_plan(
            predictions, task=task_def, cohort=event.cohort, mood=event.mood,
        )
        launched = await self.prefetch.schedule(
            scope=scope,
            session_id=event.session_id,
            task_def=task_def,
            plan=plan,
            current_turn_index=event.turn_index,
        )
        if launched:
            log.info(
                "session %s turn %d: %d speculative fetches launched",
                event.session_id, event.turn_index, launched,
            )

    async def _load_sop(self, event: TurnEvent) -> TaskDefinition | None:
        key = (event.sop_id, event.sop_version)
        if key in self._sop_cache:
            return self._sop_cache[key]
        async with get_sessionmaker()() as db:
            row = (
                await db.execute(
                    select(SopVersion).where(
                        SopVersion.sop_id == event.sop_id, SopVersion.version == event.sop_version
                    )
                )
            ).scalar_one_or_none()
        if row is None:
            log.warning("sop %s v%d not found for event", event.sop_id, event.sop_version)
            return None
        task_def = TaskDefinition.model_validate(row.definition)
        self._sop_cache[key] = task_def
        return task_def


def main() -> None:
    """Console entrypoint: `sopilot-supervisor`."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    async def _run() -> None:
        settings = get_settings()
        redis = aioredis.from_url(settings.redis_url)
        worker = SupervisorWorker(redis)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, worker.stop)
        try:
            await worker.run()
        finally:
            await redis.aclose()

    asyncio.run(_run())


if __name__ == "__main__":
    main()

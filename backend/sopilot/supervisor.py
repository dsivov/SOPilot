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
        # Milestone B: pre-draft replies for the most likely next (action, state).
        settings = get_settings()
        if settings.instruction_prefetch and scope.sop_enabled:
            try:
                await self._pregenerate_instructions(event, task_def, predictions)
            except Exception:
                log.exception("instruction pre-generation failed (best-effort)")

    async def _pregenerate_instructions(self, event: TurnEvent, task_def, predictions) -> None:
        """Draft verbatim replies for top (next-action, next-state) combos and pool
        them as kind="instruction" (exact-match consumption at plan-turn). Wrong
        guesses expire; every draft is audited like a fetch."""
        from datetime import timedelta

        from sqlalchemy import select as _select

        from .models import ConversationSession, DataFetchAudit, Turn
        from .pool import PoolItem, utcnow
        from .predictor import next_state_distribution
        from .runtime import assemble_stage_prompt

        settings = get_settings()
        scope = event.scope()
        top_actions = [p for p in predictions if p.offset == 1][:2]
        if not top_actions:
            return

        async with get_sessionmaker()() as db:
            session_row = (
                await db.execute(
                    _select(ConversationSession).where(ConversationSession.id == event.session_id)
                )
            ).scalar_one_or_none()
            turns = (
                (
                    await db.execute(
                        _select(Turn).where(Turn.session_id == event.session_id).order_by(Turn.turn_index)
                    )
                )
                .scalars()
                .all()
            )
            states = await next_state_distribution(
                db, scope, sop_id=event.sop_id, cohort=event.cohort,
                chosen_action=event.action, offset=1,
            )
            combos: list[tuple[str, str, float]] = []
            for pred in top_actions:
                for state, weight in states[:2]:
                    combos.append((pred.action, state, round(pred.probability * weight, 4)))
        combos.sort(key=lambda c: -c[2])
        combos = combos[: settings.instruction_prefetch_max_pergen]
        if not combos:
            return

        history: list[dict] = []
        for t in turns:
            if t.user_message:
                history.append({"role": "user", "content": t.user_message})
            if t.assistant_message:
                history.append({"role": "assistant", "content": t.assistant_message})
        bindings = (session_row.prompt_bindings or {}) if session_row else {}
        state_desc = {s.name: s.description for s in task_def.user_states}
        pool_items = await self.pool.get_pool(scope, event.session_id)

        from .agent import pre_generate_reply

        for action_name, state, confidence in combos:
            # already have a live draft for this exact pair? skip
            if any(
                p.kind == "instruction" and p.source_action == action_name and p.predicted_user_state == state
                for p in pool_items
            ):
                continue
            action_obj = next((a for a in task_def.agent_actions if a.name == action_name), None)
            if action_obj is None:
                continue
            stage_blocks = [bindings[n]["content"] for n in (action_obj.prompt_blocks or []) if n in bindings]
            # POC Fix A: bake the pool's relevant data into the draft so a hit
            # doesn't orphan the data path.
            deps = set(action_obj.data_dependencies or [])
            data_ctx = {
                p.dependency_name: p.payload_summary
                for p in pool_items
                if p.kind == "data" and (p.dependency_name in deps or p.source_action == action_name)
            }
            prompt_text = assemble_stage_prompt(
                task_def, action_name, dep_payloads=data_ctx or None, stage_blocks=stage_blocks
            )
            try:
                draft = await pre_generate_reply(prompt_text, history, state, state_desc.get(state, ""))
            except Exception:
                log.exception("pre-generation call failed for (%s, %s)", action_name, state)
                continue
            if not draft:
                continue
            now = utcnow()
            fetch_id = ""
            try:
                async with get_sessionmaker()() as db:
                    row = DataFetchAudit(
                        tenant_id=scope.tenant_id,
                        project_id=scope.project_id,
                        session_id=event.session_id,
                        dependency_name=f"instruction:{action_name}",
                        action_name=action_name,
                        kind="instruction",
                        speculative=True,
                        predictor_source="pregen",
                        confidence=confidence,
                        issued_at_turn=event.turn_index,
                        predicted_turn=event.turn_index + 1,
                        started_at=now,
                        completed_at=now,
                        payload_summary=draft[:500],
                    )
                    db.add(row)
                    await db.commit()
                    fetch_id = row.id
            except Exception:
                log.exception("instruction audit write failed (draft continues)")
            summary_emb = None
            try:
                summary_emb = await self.embedder.embed(draft[:200])
            except Exception:
                summary_emb = None
            await self.pool.insert(
                scope,
                event.session_id,
                PoolItem(
                    fetch_id=fetch_id,
                    fetched_at=now,
                    expires_at=now + timedelta(seconds=settings.instruction_ttl_s),
                    dependency_name=f"instruction:{action_name}",
                    source_action=action_name,
                    payload=draft,
                    payload_summary=draft[:200],
                    confidence=confidence,
                    predictor_source="pregen",
                    predicted_user_state=state,
                    kind="instruction",
                    instr_data_count=len(data_ctx),
                    summary_embedding=summary_emb,
                ),
            )
            log.info(
                "session %s: pre-drafted reply for (%s, %s) conf=%.3f data=%d",
                event.session_id, action_name, state, confidence, len(data_ctx),
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

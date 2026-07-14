"""Supervisor worker: consume turn events → predict → prefetch → pool, with the
predictor and SOP loading stubbed (no Postgres needed)."""
import asyncio

import pytest

from sopilot.events import TURN_STREAM, publish_turn_event
from sopilot.fetchers import MockFetcher, register_fetcher
from sopilot.pool import SessionPool
from sopilot.predictor import TrajectoryPrediction
from sopilot.prefetch import PrefetchManager
from sopilot.supervisor import SupervisorWorker
from sopilot.schemas import DataDependency, NamedItem, TaskDefinition

from test_events import make_event


def make_task_def() -> TaskDefinition:
    return TaskDefinition(
        name="renewal",
        agent_actions=[NamedItem(name="PitchRenewal", data_dependencies=["policy"])],
        data_dependencies=[
            DataDependency(name="policy", kind="mock", expected_latency_ms=5, config={"text": "policy #9"})
        ],
    )


@pytest.fixture
async def worker(redis, embedder, monkeypatch):
    register_fetcher("mock", MockFetcher())
    # Avoid Settings/OpenAI/Postgres: construct without __init__ and wire by hand.
    w = SupervisorWorker.__new__(SupervisorWorker)
    w.redis = redis
    w.group = "supervisor"
    w.consumer = "test-worker"
    w.block_ms = 10
    w.batch = 10
    w.autoclaim_idle_ms = 60000
    w.embedder = embedder
    w.pool = SessionPool(redis, max_items=10, session_ttl_s=600)
    w.prefetch = PrefetchManager(w.pool, _failing_sessionmaker, embedder)
    w._stop = asyncio.Event()
    w._sop_cache = {}

    async def fake_load_sop(event):
        return make_task_def()

    async def fake_predict_traj(event):
        return [TrajectoryPrediction(action="PitchRenewal", offset=1, probability=0.9)]

    w._load_sop = fake_load_sop
    w._predictions_for = fake_predict_traj
    return w


def _failing_sessionmaker():
    raise RuntimeError("no db in unit test")  # audit path must swallow this


async def test_event_triggers_prefetch_into_pool(worker, redis, scope_a):
    await publish_turn_event(redis, make_event(subsystems="both", session_id="sessX"))
    # patch the predictor call inside _predict_and_prefetch by overriding it fully:
    orig = worker._predict_and_prefetch

    async def predict_and_prefetch(event):
        task_def = await worker._load_sop(event)
        preds = await worker._predictions_for(event)
        from sopilot.predictor import build_prefetch_plan

        plan = build_prefetch_plan(preds, task=task_def, cohort=event.cohort, mood=event.mood)
        await worker.prefetch.schedule(
            scope=event.scope(), session_id=event.session_id, task_def=task_def,
            plan=plan, current_turn_index=event.turn_index,
        )

    worker._predict_and_prefetch = predict_and_prefetch

    from sopilot.events import ensure_group
    await ensure_group(redis, worker.group)
    resp = await redis.xreadgroup(worker.group, worker.consumer, {TURN_STREAM: ">"}, count=10)
    for _stream, entries in resp:
        for entry_id, fields in entries:
            await worker._handle(entry_id, fields)
    await asyncio.gather(*worker.prefetch._tasks, return_exceptions=True)

    pool_items = await worker.pool.get_pool(make_event(session_id="sessX").scope(), "sessX")
    assert [p.dependency_name for p in pool_items] == ["policy"]
    # event acked
    pending = await redis.xpending(TURN_STREAM, worker.group)
    assert pending["pending"] == 0
    del orig


async def test_sop_only_event_does_no_retrieval_work(worker, redis):
    called = False

    async def spy(event):
        nonlocal called
        called = True

    worker._predict_and_prefetch = spy
    await publish_turn_event(redis, make_event(subsystems="sop", session_id="sessY"))
    from sopilot.events import ensure_group
    await ensure_group(redis, worker.group)
    resp = await redis.xreadgroup(worker.group, worker.consumer, {TURN_STREAM: ">"}, count=10)
    for _stream, entries in resp:
        for entry_id, fields in entries:
            await worker._handle(entry_id, fields)
    assert called is False  # retrieval disabled → supervisor skips, but still acks
    pending = await redis.xpending(TURN_STREAM, worker.group)
    assert pending["pending"] == 0


async def test_poison_event_is_dropped_not_retried_forever(worker, redis):
    async def boom(event):
        raise RuntimeError("predictor exploded")

    worker._predict_and_prefetch = boom
    await publish_turn_event(redis, make_event(subsystems="both"))
    from sopilot.events import ensure_group
    await ensure_group(redis, worker.group)
    resp = await redis.xreadgroup(worker.group, worker.consumer, {TURN_STREAM: ">"}, count=10)
    for _stream, entries in resp:
        for entry_id, fields in entries:
            await worker._handle(entry_id, fields)  # must not raise
    pending = await redis.xpending(TURN_STREAM, worker.group)
    assert pending["pending"] == 0  # acked despite failure — dropped, logged

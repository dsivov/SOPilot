"""Prefetch manager unit tests: fetchers + fakeredis pool; DB audit is stubbed out
by pointing the sessionmaker at a throwaway in-memory SQLite (audit best-effort:
the manager must work even when audit writes fail)."""
import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sopilot.fetchers import MockFetcher, register_fetcher
from sopilot.models import Base
from sopilot.pool import SessionPool
from sopilot.predictor import PrefetchPlanItem
from sopilot.prefetch import PrefetchManager
from sopilot.schemas import DataDependency, NamedItem, TaskDefinition


def make_task() -> TaskDefinition:
    return TaskDefinition(
        name="t",
        agent_actions=[NamedItem(name="PitchRenewal", data_dependencies=["policy", "booking"])],
        data_dependencies=[
            DataDependency(name="policy", kind="mock", expected_latency_ms=30, cache_ttl_s=60,
                           config={"text": "policy #123 details"}),
            DataDependency(name="booking", kind="mock", expected_latency_ms=10, idempotent=False),
        ],
    )


@pytest.fixture
async def sqlite_sessionmaker():
    # models use pgvector Vector — SQLite can't create that table, but the audit path
    # swallows failures by design; create only what compiles, ignore the rest.
    engine = create_async_engine("sqlite+aiosqlite://")
    sm = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except Exception:
        pass
    yield sm
    await engine.dispose()


@pytest.fixture
def manager(redis, embedder, sqlite_sessionmaker):
    register_fetcher("mock", MockFetcher())
    pool = SessionPool(redis, max_items=10, session_ttl_s=600)
    return PrefetchManager(pool, sqlite_sessionmaker, embedder)


async def test_schedule_skips_non_idempotent_and_low_confidence(manager, scope_a):
    plan = [
        PrefetchPlanItem("policy", "PitchRenewal", confidence=0.8, predicted_turn_offset=1),
        PrefetchPlanItem("booking", "PitchRenewal", confidence=0.9, predicted_turn_offset=1),  # non-idempotent
        PrefetchPlanItem("policy", "PitchRenewal", confidence=0.01, predicted_turn_offset=2),  # below floor
    ]
    launched = await manager.schedule(
        scope=scope_a, session_id="s1", task_def=make_task(), plan=plan, current_turn_index=0
    )
    assert launched == 1  # only the confident, idempotent policy fetch fired
    await asyncio.gather(*manager._tasks, return_exceptions=True)
    pool_items = await manager.pool.get_pool(scope_a, "s1")
    assert [p.dependency_name for p in pool_items] == ["policy"]
    assert pool_items[0].payload == "policy #123 details"
    assert pool_items[0].summary_embedding is not None


async def test_schedule_dedups_across_calls(manager, scope_a):
    plan = [PrefetchPlanItem("policy", "PitchRenewal", confidence=0.8, predicted_turn_offset=1)]
    first = await manager.schedule(
        scope=scope_a, session_id="s1", task_def=make_task(), plan=plan, current_turn_index=0
    )
    second = await manager.schedule(
        scope=scope_a, session_id="s1", task_def=make_task(), plan=plan, current_turn_index=0
    )
    assert (first, second) == (1, 0)  # in-flight claim blocks the duplicate
    await asyncio.gather(*manager._tasks, return_exceptions=True)


async def test_consume_hits_pool_then_falls_back_live(manager, scope_a):
    task_def = make_task()
    plan = [PrefetchPlanItem("policy", "PitchRenewal", confidence=0.8, predicted_turn_offset=1)]
    await manager.schedule(scope=scope_a, session_id="s1", task_def=task_def, plan=plan, current_turn_index=0)
    await asyncio.gather(*manager._tasks, return_exceptions=True)

    payloads, stats = await manager.consume(
        scope=scope_a, session_id="s1", task_def=task_def, action_name="PitchRenewal",
        current_turn_index=1, await_inflight_ms=0,
    )
    # policy was prefetched (hit); booking is non-idempotent → live fetch
    assert stats["consumed"] == 1
    assert stats["live"] == 1
    assert "policy" in payloads and "booking" in payloads


async def test_finalize_clears_pool(manager, scope_a):
    plan = [PrefetchPlanItem("policy", "PitchRenewal", confidence=0.8, predicted_turn_offset=1)]
    await manager.schedule(
        scope=scope_a, session_id="s1", task_def=make_task(), plan=plan, current_turn_index=0
    )
    await asyncio.gather(*manager._tasks, return_exceptions=True)
    await manager.finalize_session(scope_a, "s1")
    assert await manager.pool.get_pool(scope_a, "s1") == []

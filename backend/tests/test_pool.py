from datetime import timedelta

from sopilot.pool import PoolItem, utcnow


def make_item(dep: str, conf: float, *, kind: str = "data", ttl_s: int = 300, **kw) -> PoolItem:
    now = utcnow()
    return PoolItem(
        dependency_name=dep,
        source_action=kw.pop("action", "ActA"),
        payload=f"payload-{dep}",
        payload_summary=f"summary-{dep}",
        confidence=conf,
        fetched_at=now,
        expires_at=now + timedelta(seconds=ttl_s),
        **kw,
    ) if kind == "data" else PoolItem(
        dependency_name=dep,
        source_action=kw.pop("action", "ActA"),
        payload=f"payload-{dep}",
        payload_summary=f"summary-{dep}",
        confidence=conf,
        fetched_at=now,
        expires_at=now + timedelta(seconds=ttl_s),
        kind=kind,
        **kw,
    )


async def test_insert_and_roundtrip(pool, scope_a, unit_vec):
    item = make_item("dep1", 0.7)
    item.summary_embedding = unit_vec(1)
    await pool.insert(scope_a, "sess1", item)
    got = await pool.get_pool(scope_a, "sess1")
    assert len(got) == 1
    p = got[0]
    assert p.dependency_name == "dep1"
    assert p.payload == "payload-dep1"
    assert p.confidence == 0.7
    assert p.summary_embedding is not None and p.summary_embedding.shape == (32,)


async def test_ttl_expiry(pool, scope_a):
    await pool.insert(scope_a, "sess1", make_item("fresh", 0.5, ttl_s=300))
    await pool.insert(scope_a, "sess1", make_item("stale", 0.9, ttl_s=-1))  # already expired
    got = await pool.get_pool(scope_a, "sess1")
    assert [p.dependency_name for p in got] == ["fresh"]


async def test_cap_evicts_lowest_confidence(pool, scope_a):
    # pool fixture caps at 5
    for i in range(6):
        await pool.insert(scope_a, "sess1", make_item(f"dep{i}", conf=i / 10))
    got = await pool.get_pool(scope_a, "sess1")
    assert len(got) == 5
    names = {p.dependency_name for p in got}
    assert "dep0" not in names  # lowest confidence evicted


async def test_tenant_isolation(pool, scope_a, scope_b):
    await pool.insert(scope_a, "sess1", make_item("secretA", 0.9))
    assert await pool.get_pool(scope_b, "sess1") == []  # same session id, different tenant
    got_a = await pool.get_pool(scope_a, "sess1")
    assert len(got_a) == 1


async def test_instruction_lookup_exact_match(pool, scope_a):
    instr = make_item("instruction:PitchRenewal", 0.6, kind="instruction", action="PitchRenewal")
    instr.predicted_user_state = "Interested"
    await pool.insert(scope_a, "sess1", instr)
    hit = await pool.lookup_instruction(
        scope_a, "sess1", chosen_action="PitchRenewal", classified_state="Interested"
    )
    assert hit is not None and hit.kind == "instruction"
    miss = await pool.lookup_instruction(
        scope_a, "sess1", chosen_action="PitchRenewal", classified_state="Objecting"
    )
    assert miss is None


async def test_inflight_claim_is_exclusive(pool, scope_a):
    assert await pool.try_claim_fetch(scope_a, "sess1", "key1") is True
    assert await pool.try_claim_fetch(scope_a, "sess1", "key1") is False  # second worker loses
    assert await pool.is_inflight(scope_a, "sess1", "key1") is True
    await pool.release_fetch(scope_a, "sess1", "key1")
    assert await pool.is_inflight(scope_a, "sess1", "key1") is False


async def test_clear(pool, scope_a):
    await pool.insert(scope_a, "sess1", make_item("dep1", 0.5))
    await pool.clear(scope_a, "sess1")
    assert await pool.get_pool(scope_a, "sess1") == []

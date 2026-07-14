from sopilot.events import TURN_STREAM, TurnEvent, ensure_group, publish_turn_event, stream_lag_ms


def make_event(**kw) -> TurnEvent:
    defaults = dict(
        tenant_id="tenantA",
        project_id="projA",
        subsystems="both",
        session_id="sess1",
        sop_id="sop1",
        sop_version=1,
        turn_index=2,
        user_message="what about the premium?",
        cohort="PriceShopper",
        mood="irritated",
        state="Objecting",
        action="HandleObjection",
    )
    defaults.update(kw)
    return TurnEvent(**defaults)


async def test_publish_and_read_roundtrip(redis):
    event = make_event()
    entry_id = await publish_turn_event(redis, event)
    assert entry_id
    entries = await redis.xrange(TURN_STREAM)
    assert len(entries) == 1
    parsed = TurnEvent.from_fields(entries[0][1])
    assert parsed == event
    assert parsed.scope().redis_prefix() == "sop:tenantA:projA"
    assert parsed.scope().retrieval_enabled and parsed.scope().sop_enabled


async def test_consumer_group_ack_flow(redis):
    await ensure_group(redis, "supervisor")
    await ensure_group(redis, "supervisor")  # idempotent (BUSYGROUP swallowed)
    await publish_turn_event(redis, make_event(turn_index=0))
    await publish_turn_event(redis, make_event(turn_index=1))
    resp = await redis.xreadgroup("supervisor", "w1", {TURN_STREAM: ">"}, count=10)
    entries = resp[0][1]
    assert len(entries) == 2
    # unacked entries stay pending; after ack they clear
    pending = await redis.xpending(TURN_STREAM, "supervisor")
    assert pending["pending"] == 2
    for entry_id, _ in entries:
        await redis.xack(TURN_STREAM, "supervisor", entry_id)
    pending = await redis.xpending(TURN_STREAM, "supervisor")
    assert pending["pending"] == 0


async def test_stream_lag(redis):
    await ensure_group(redis, "supervisor")
    assert await stream_lag_ms(redis, "supervisor") == 0
    await publish_turn_event(redis, make_event())
    await redis.xreadgroup("supervisor", "w1", {TURN_STREAM: ">"}, count=1)
    lag = await stream_lag_ms(redis, "supervisor")
    assert lag is None or lag >= 0  # fakeredis TIME support varies; must not crash


async def test_mode_fields_survive_roundtrip(redis):
    await publish_turn_event(redis, make_event(subsystems="sop"))
    entries = await redis.xrange(TURN_STREAM)
    parsed = TurnEvent.from_fields(entries[-1][1])
    assert parsed.scope().sop_enabled is True
    assert parsed.scope().retrieval_enabled is False

"""Turn-event bus (D-1): the only signal from the online lane to the supervisor.

One global Redis Stream + one consumer group. Tenancy rides in the payload (the
supervisor re-derives a Scope from it); pool keys stay scoped as always. A single
stream keeps operations simple: one lag metric, one XAUTOCLAIM sweep, workers
scale by joining the group.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import redis.asyncio as aioredis

from .tenancy import Scope

TURN_STREAM = "sopilot:events:turns"


@dataclass
class TurnEvent:
    tenant_id: str
    project_id: str
    subsystems: str
    session_id: str
    sop_id: str
    sop_version: int
    turn_index: int
    user_message: str
    cohort: str = ""
    mood: str = ""
    state: str = ""
    action: str = ""

    def scope(self) -> Scope:
        return Scope(tenant_id=self.tenant_id, project_id=self.project_id, subsystems=self.subsystems)

    def to_fields(self) -> dict[str, str]:
        d = asdict(self)
        return {k: str(v) for k, v in d.items()}

    @classmethod
    def from_fields(cls, fields: dict) -> "TurnEvent":
        get = lambda k: fields.get(k) if k in fields else fields.get(k.encode())  # noqa: E731
        s = lambda k: (get(k) or b"").decode() if isinstance(get(k), bytes) else (get(k) or "")  # noqa: E731
        return cls(
            tenant_id=s("tenant_id"),
            project_id=s("project_id"),
            subsystems=s("subsystems") or "both",
            session_id=s("session_id"),
            sop_id=s("sop_id"),
            sop_version=int(s("sop_version") or 0),
            turn_index=int(s("turn_index") or 0),
            user_message=s("user_message"),
            cohort=s("cohort"),
            mood=s("mood"),
            state=s("state"),
            action=s("action"),
        )


async def publish_turn_event(redis: aioredis.Redis, event: TurnEvent) -> str:
    """Fire-and-forget from the online lane's perspective (one XADD, ~50 µs)."""
    entry_id = await redis.xadd(TURN_STREAM, event.to_fields(), maxlen=100_000, approximate=True)
    return entry_id.decode() if isinstance(entry_id, bytes) else entry_id


async def ensure_group(redis: aioredis.Redis, group: str) -> None:
    try:
        await redis.xgroup_create(TURN_STREAM, group, id="0", mkstream=True)
    except aioredis.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise


async def stream_lag_ms(redis: aioredis.Redis, group: str) -> int | None:
    """Age of the oldest pending entry — the supervisor-lag SLI. None = no backlog."""
    try:
        pending = await redis.xpending(TURN_STREAM, group)
        if not pending or not pending.get("pending"):
            return 0
        oldest = pending.get("min")
        if not oldest:
            return 0
        oldest_ms = int((oldest.decode() if isinstance(oldest, bytes) else oldest).split("-")[0])
        server_ms = int((await redis.time())[0]) * 1000
        return max(0, server_ms - oldest_ms)
    except Exception:
        return None

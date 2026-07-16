"""SLI aggregates for the operations dashboard — computed from the audit tables
the runtime has written since day one (data_fetches, pool_picks, sessions, turns).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_db
from ..events import stream_lag_ms
from ..models import ConversationSession, DataFetchAudit, PoolPickAudit, Turn
from ..tenancy import Scope, resolve_scope

router = APIRouter(prefix="/metrics", tags=["metrics"])


@router.get("/summary")
async def summary(
    request: Request,
    scope: Scope = Depends(resolve_scope),
    db: AsyncSession = Depends(get_db),
    days: int = 7,
) -> dict:
    from datetime import UTC, datetime, timedelta

    since = datetime.now(UTC) - timedelta(days=max(1, min(days, 90)))

    scoped_fetch = [
        DataFetchAudit.tenant_id == scope.tenant_id,
        DataFetchAudit.project_id == scope.project_id,
        DataFetchAudit.created_at >= since,
    ]

    # data fetches: speculative vs live, consumed vs wasted, hidden latency
    fetch_row = (
        await db.execute(
            select(
                func.count().filter(DataFetchAudit.kind != "instruction"),
                func.count().filter(
                    DataFetchAudit.kind != "instruction",
                    DataFetchAudit.speculative.is_(True),
                    DataFetchAudit.consumed.is_(True),
                ),
                func.count().filter(
                    DataFetchAudit.kind != "instruction", DataFetchAudit.speculative.is_(False)
                ),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                (DataFetchAudit.speculative.is_(True))
                                & (DataFetchAudit.consumed.is_(True))
                                & (DataFetchAudit.kind != "instruction"),
                                DataFetchAudit.fetch_duration_ms,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ),
            ).where(*scoped_fetch)
        )
    ).one()
    total_fetches, spec_consumed, live_fetches, hidden_ms = (
        int(fetch_row[0]), int(fetch_row[1]), int(fetch_row[2]), int(fetch_row[3]),
    )
    consumed_total = spec_consumed + live_fetches

    # instruction drafts
    instr_row = (
        await db.execute(
            select(
                func.count(),
                func.count().filter(DataFetchAudit.consumed.is_(True)),
            ).where(*scoped_fetch, DataFetchAudit.kind == "instruction")
        )
    ).one()
    instr_drafts, instr_consumed = int(instr_row[0]), int(instr_row[1])

    # turn-level instruction hit rate (a draft can serve multiple turns)
    turn_row = (
        await db.execute(
            select(
                func.count(),
                func.count().filter(Turn.instruction_hit.is_(True)),
            )
            .select_from(Turn)
            .join(ConversationSession, ConversationSession.id == Turn.session_id)
            .where(
                ConversationSession.tenant_id == scope.tenant_id,
                ConversationSession.project_id == scope.project_id,
                Turn.turn_index >= 1,
                Turn.created_at >= since,
            )
        )
    ).one()
    eligible_turns, turn_hits = int(turn_row[0]), int(turn_row[1])

    # rerank latency percentiles
    pick_ms = sorted(
        r[0]
        for r in (
            await db.execute(
                select(PoolPickAudit.pick_duration_ms).where(
                    PoolPickAudit.tenant_id == scope.tenant_id,
                    PoolPickAudit.project_id == scope.project_id,
                    PoolPickAudit.created_at >= since,
                )
            )
        ).all()
    )
    pick_turns = len(pick_ms)
    picked_some = (
        await db.execute(
            select(func.count()).where(
                PoolPickAudit.tenant_id == scope.tenant_id,
                PoolPickAudit.project_id == scope.project_id,
                PoolPickAudit.created_at >= since,
                func.json_array_length(PoolPickAudit.picked_item_ids) > 0,
            )
        )
    ).scalar_one()

    def pct(xs: list, q: float) -> int:
        return int(xs[min(len(xs) - 1, int(q * len(xs)))]) if xs else 0

    # sessions by outcome
    sess_rows = (
        await db.execute(
            select(ConversationSession.status, ConversationSession.terminal_outcome, func.count())
            .where(
                ConversationSession.tenant_id == scope.tenant_id,
                ConversationSession.project_id == scope.project_id,
                ConversationSession.started_at >= since,
            )
            .group_by(ConversationSession.status, ConversationSession.terminal_outcome)
        )
    ).all()
    # an ended session with no terminal outcome is "no_outcome", not in-progress
    outcomes: dict[str, int] = {}
    for status, outcome, n in sess_rows:
        label = outcome or ("in_progress" if status == "active" else "no_outcome")
        outcomes[label] = outcomes.get(label, 0) + int(n)
    n_sessions = sum(outcomes.values())

    lag = await stream_lag_ms(request.app.state.redis, get_settings().supervisor_group)

    return {
        "window_days": days,
        "sessions": {"total": n_sessions, "by_outcome": outcomes},
        "data": {
            "fetches": total_fetches,
            "speculative_hit_rate": round(spec_consumed / consumed_total, 3) if consumed_total else None,
            "live_fallback_rate": round(live_fetches / consumed_total, 3) if consumed_total else None,
            "latency_hidden_ms_total": hidden_ms,
            "latency_hidden_s_per_session": round(hidden_ms / 1000 / n_sessions, 1) if n_sessions else 0,
        },
        "instructions": {
            "drafts": instr_drafts,
            "drafts_served": instr_consumed,
            "turn_hits": turn_hits,
            "hit_rate_vs_eligible_turns": round(turn_hits / eligible_turns, 3) if eligible_turns else None,
            "draft_efficiency": round(instr_consumed / instr_drafts, 3) if instr_drafts else None,
        },
        "selection": {
            "turns_with_rerank": pick_turns,
            "pick_rate": round(picked_some / pick_turns, 3) if pick_turns else None,
            "rerank_ms_p50": pct(pick_ms, 0.50),
            "rerank_ms_p95": pct(pick_ms, 0.95),
        },
        "supervisor_lag_ms": lag,
    }

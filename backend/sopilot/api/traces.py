"""Read-only browser over precedent traces — the predictor's training data.

Real conversations distilled to (situation → action → outcome) rows. The
Studio's Traces view uses this to answer "what has the system actually
learned from" per SOP.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import PrecedentTrace, Sop
from ..tenancy import Scope, resolve_scope

router = APIRouter(prefix="/traces", tags=["traces"])


@router.get("/summary")
async def trace_summary(
    scope: Scope = Depends(resolve_scope), db: AsyncSession = Depends(get_db)
) -> dict:
    """Per-SOP totals + outcome mix — the browser's header strip."""
    rows = (
        await db.execute(
            select(
                PrecedentTrace.sop_id,
                Sop.name,
                PrecedentTrace.terminal_outcome,
                func.count(),
            )
            .join(Sop, Sop.id == PrecedentTrace.sop_id)
            .where(
                PrecedentTrace.tenant_id == scope.tenant_id,
                PrecedentTrace.project_id == scope.project_id,
            )
            .group_by(PrecedentTrace.sop_id, Sop.name, PrecedentTrace.terminal_outcome)
        )
    ).all()
    sops: dict[str, dict] = {}
    for sop_id, sop_name, outcome, n in rows:
        entry = sops.setdefault(sop_id, {"sop_id": sop_id, "sop_name": sop_name, "total": 0, "by_outcome": {}})
        entry["total"] += int(n)
        entry["by_outcome"][outcome or "in_progress"] = int(n)
    return {"sops": sorted(sops.values(), key=lambda s: -s["total"])}


@router.get("")
async def list_traces(
    scope: Scope = Depends(resolve_scope),
    db: AsyncSession = Depends(get_db),
    sop_id: str | None = None,
    action: str | None = None,
    outcome: str | None = None,
    session_id: str | None = None,
    q: str | None = Query(None, description="substring match on response_text"),
    limit: int = Query(50, le=200),
    offset: int = 0,
) -> dict:
    where = [
        PrecedentTrace.tenant_id == scope.tenant_id,
        PrecedentTrace.project_id == scope.project_id,
    ]
    if sop_id:
        where.append(PrecedentTrace.sop_id == sop_id)
    if action:
        where.append(PrecedentTrace.action == action)
    if outcome:
        where.append(
            PrecedentTrace.terminal_outcome.is_(None)
            if outcome == "in_progress"
            else PrecedentTrace.terminal_outcome == outcome
        )
    if session_id:
        where.append(PrecedentTrace.session_id == session_id)
    if q:
        where.append(PrecedentTrace.response_text.ilike(f"%{q}%"))

    total = (await db.execute(select(func.count()).select_from(PrecedentTrace).where(*where))).scalar_one()
    rows = (
        (
            await db.execute(
                select(PrecedentTrace)
                .where(*where)
                .order_by(PrecedentTrace.created_at.desc(), PrecedentTrace.turn_index.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return {
        "total": total,
        "items": [
            {
                "id": t.id,
                "sop_id": t.sop_id,
                "session_id": t.session_id,
                "turn_index": t.turn_index,
                "cohort": t.cohort,
                "mood": t.mood,
                "action": t.action,
                "immediate_state": t.immediate_state,
                "terminal_outcome": t.terminal_outcome,
                "terminal_reward": t.terminal_reward,
                "turn_distance_to_terminal": t.turn_distance_to_terminal,
                "response_text": t.response_text,
                "has_embedding": t.situation_embedding is not None,
                "created_at": t.created_at.isoformat(),
            }
            for t in rows
        ],
    }


@router.get("/facets")
async def trace_facets(
    scope: Scope = Depends(resolve_scope),
    db: AsyncSession = Depends(get_db),
    sop_id: str | None = None,
) -> dict:
    """Distinct actions/outcomes for filter dropdowns (scoped to one SOP if given)."""
    where = [
        PrecedentTrace.tenant_id == scope.tenant_id,
        PrecedentTrace.project_id == scope.project_id,
    ]
    if sop_id:
        where.append(PrecedentTrace.sop_id == sop_id)
    actions = (
        (await db.execute(select(PrecedentTrace.action, func.count()).where(*where).group_by(PrecedentTrace.action)))
        .all()
    )
    outcomes = (
        (
            await db.execute(
                select(PrecedentTrace.terminal_outcome, func.count())
                .where(*where)
                .group_by(PrecedentTrace.terminal_outcome)
            )
        )
        .all()
    )
    return {
        "actions": [{"name": a, "count": int(n)} for a, n in sorted(actions, key=lambda r: -r[1])],
        "outcomes": [{"name": o or "in_progress", "count": int(n)} for o, n in outcomes],
    }

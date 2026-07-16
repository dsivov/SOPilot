"""Conversation session lifecycle + pool inspection (the console's live X-ray)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import ConversationSession, Sop, SopVersion, utcnow
from ..schemas import SessionStartRequest, SessionStartResponse, TaskDefinition
from ..tenancy import Scope, resolve_scope

router = APIRouter(prefix="/sessions", tags=["sessions"])


async def _get_session(db: AsyncSession, scope: Scope, session_id: str) -> ConversationSession:
    row = (
        await db.execute(
            select(ConversationSession).where(
                ConversationSession.id == session_id,
                ConversationSession.tenant_id == scope.tenant_id,
                ConversationSession.project_id == scope.project_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    return row


@router.get("")
async def list_sessions(
    scope: Scope = Depends(resolve_scope), db: AsyncSession = Depends(get_db), limit: int = 50
) -> list[dict]:
    rows = (
        (
            await db.execute(
                select(ConversationSession)
                .where(
                    ConversationSession.tenant_id == scope.tenant_id,
                    ConversationSession.project_id == scope.project_id,
                )
                .order_by(ConversationSession.started_at.desc())
                .limit(min(limit, 200))
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "session_id": s.id,
            "sop_id": s.sop_id,
            "sop_version": s.sop_version,
            "channel": s.channel,
            "subsystems": s.subsystems_override or scope.subsystems,
            "status": s.status,
            "terminal_outcome": s.terminal_outcome,
            "started_at": s.started_at.isoformat(),
        }
        for s in rows
    ]


@router.post("", response_model=SessionStartResponse)
async def start_session(
    req: SessionStartRequest,
    scope: Scope = Depends(resolve_scope),
    db: AsyncSession = Depends(get_db),
) -> SessionStartResponse:
    if not req.sop_id:
        # D-11 intake session: no SOP yet — the router assigns one in /converse
        # on the first routable utterance.
        session = ConversationSession(
            tenant_id=scope.tenant_id,
            project_id=scope.project_id,
            sop_id=None,
            sop_version=0,
            channel=req.channel,
            subsystems_override=req.subsystems,
        )
        db.add(session)
        await db.commit()
        return SessionStartResponse(session_id=session.id, routed=False)

    sop = (
        await db.execute(
            select(Sop).where(
                Sop.id == req.sop_id, Sop.tenant_id == scope.tenant_id, Sop.project_id == scope.project_id
            )
        )
    ).scalar_one_or_none()
    if sop is None:
        raise HTTPException(status_code=404, detail="SOP not found")
    if req.sop_version:
        version = (
            await db.execute(
                select(SopVersion).where(
                    SopVersion.sop_id == sop.id, SopVersion.version == req.sop_version
                )
            )
        ).scalar_one_or_none()
        if version is None:
            raise HTTPException(status_code=404, detail=f"SOP version {req.sop_version} not found")
    else:
        version = (
            await db.execute(
                select(SopVersion)
                .where(SopVersion.sop_id == sop.id, SopVersion.status == "published")
                .order_by(SopVersion.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if version is None:
            raise HTTPException(status_code=409, detail="SOP has no published version")
    # D-7: pin prompt-block bindings now — a block published mid-conversation
    # never lands mid-call.
    from ..runtime import collect_prompt_block_names
    from .prompt_blocks import resolve_published_blocks

    task_def = TaskDefinition.model_validate(version.definition)
    bindings, missing = await resolve_published_blocks(db, scope, collect_prompt_block_names(task_def))
    if missing:
        raise HTTPException(
            status_code=409,
            detail={"message": "SOP binds prompt blocks with no published version", "missing": sorted(missing)},
        )
    session = ConversationSession(
        tenant_id=scope.tenant_id,
        project_id=scope.project_id,
        sop_id=sop.id,
        sop_version=version.version,
        channel=req.channel,
        subsystems_override=req.subsystems,
        prompt_bindings=bindings or None,
    )
    db.add(session)
    await db.commit()
    return SessionStartResponse(
        session_id=session.id,
        sop_version=version.version,
        definition=TaskDefinition.model_validate(version.definition),
    )


@router.get("/{session_id}/pool")
async def get_pool_snapshot(
    session_id: str,
    request: Request,
    scope: Scope = Depends(resolve_scope),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _get_session(db, scope, session_id)
    items = await request.app.state.pool.get_pool(scope, session_id)
    return {
        "session_id": session_id,
        "size": len(items),
        "items": [
            {
                "item_id": p.item_id,
                "kind": p.kind,
                "dependency_name": p.dependency_name,
                "source_action": p.source_action,
                "payload_summary": p.payload_summary,
                "confidence": p.confidence,
                "predictor_source": p.predictor_source,
                "predicted_user_state": p.predicted_user_state,
                "fetched_at": p.fetched_at.isoformat(),
                "expires_at": p.expires_at.isoformat(),
            }
            for p in items
        ],
    }


@router.get("/{session_id}/fetches")
async def get_fetch_audit(
    session_id: str,
    scope: Scope = Depends(resolve_scope),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The permanent record of what the supervisor did for this session — unlike
    the live pool (which is cleared at session end and TTL-bound), this survives."""
    from ..models import DataFetchAudit

    await _get_session(db, scope, session_id)
    rows = (
        (
            await db.execute(
                select(DataFetchAudit)
                .where(
                    DataFetchAudit.tenant_id == scope.tenant_id,
                    DataFetchAudit.session_id == session_id,
                )
                .order_by(DataFetchAudit.created_at)
            )
        )
        .scalars()
        .all()
    )
    return {
        "session_id": session_id,
        "fetches": [
            {
                "kind": r.kind,
                "connector": r.connector,
                "dependency_name": r.dependency_name,
                "action_name": r.action_name,
                "predictor_source": r.predictor_source,
                "speculative": r.speculative,
                "consumed": r.consumed,
                "wasted": r.wasted,
                "confidence": r.confidence,
                "fetch_duration_ms": r.fetch_duration_ms,
                "issued_at_turn": r.issued_at_turn,
                "consumed_at_turn": r.consumed_at_turn,
                "payload_summary": (r.payload_summary or "")[:120],
                "error": bool(r.fetch_error),
            }
            for r in rows
        ],
    }


@router.get("/{session_id}/journey")
async def session_journey(
    session_id: str, scope: Scope = Depends(resolve_scope), db: AsyncSession = Depends(get_db)
) -> dict:
    """The conversation mapped onto its SOP graph: the pinned definition, every
    turn (with the state/action the tracker assigned), and the prompt-block
    wording that was pinned for this session. Powers the Sessions journey panel."""
    from ..models import Turn

    session = await _get_session(db, scope, session_id)
    version = (
        await db.execute(
            select(SopVersion).where(
                SopVersion.sop_id == session.sop_id, SopVersion.version == session.sop_version
            )
        )
    ).scalar_one_or_none()
    turns = (
        (
            await db.execute(
                select(Turn).where(Turn.session_id == session.id).order_by(Turn.turn_index.asc())
            )
        )
        .scalars()
        .all()
    )
    from ..models import RoutingEvent

    routing = (
        (
            await db.execute(
                select(RoutingEvent)
                .where(RoutingEvent.session_id == session.id)
                .order_by(RoutingEvent.created_at)
            )
        )
        .scalars()
        .all()
    )
    return {
        "session_id": session.id,
        "sop_id": session.sop_id,
        "sop_version": session.sop_version,
        "status": session.status,
        "terminal_outcome": session.terminal_outcome,
        "routing_events": [
            {"turn_index": e.turn_index, "kind": e.kind, "chosen_sop_id": e.chosen_sop_id,
             "previous_sop_id": e.previous_sop_id, "reason": e.reason, "router_ms": e.router_ms}
            for e in routing
        ],
        "definition": version.definition if version else None,
        "prompt_bindings": session.prompt_bindings or {},
        "turns": [
            {
                "turn_index": t.turn_index,
                "user_message": t.user_message,
                "assistant_message": t.assistant_message,
                "state": t.state,
                "action": t.action,
                "cohort": t.cohort,
                "mood": t.mood,
                "instruction_hit": t.instruction_hit,
                "duration_ms": t.duration_ms,
                "debug": t.debug,
                "created_at": t.created_at.isoformat(),
            }
            for t in turns
        ],
    }


@router.post("/{session_id}/end")
async def end_session(
    session_id: str,
    request: Request,
    scope: Scope = Depends(resolve_scope),
    db: AsyncSession = Depends(get_db),
) -> dict:
    session = await _get_session(db, scope, session_id)
    session.status = "ended"
    session.ended_at = utcnow()
    await db.commit()
    await request.app.state.prefetch.finalize_session(scope, session_id)
    return {"session_id": session_id, "status": "ended"}

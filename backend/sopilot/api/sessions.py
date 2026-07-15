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


@router.post("", response_model=SessionStartResponse)
async def start_session(
    req: SessionStartRequest,
    scope: Scope = Depends(resolve_scope),
    db: AsyncSession = Depends(get_db),
) -> SessionStartResponse:
    sop = (
        await db.execute(
            select(Sop).where(
                Sop.id == req.sop_id, Sop.tenant_id == scope.tenant_id, Sop.project_id == scope.project_id
            )
        )
    ).scalar_one_or_none()
    if sop is None:
        raise HTTPException(status_code=404, detail="SOP not found")
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

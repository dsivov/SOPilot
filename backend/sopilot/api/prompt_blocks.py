"""Prompt-block library (D-7): versioned like SOPs, published like SOPs.
Legal updates the disclosure text here without touching any conversation graph.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import PromptBlock, PromptBlockVersion, utcnow
from ..tenancy import Scope, resolve_scope

router = APIRouter(prefix="/prompt-blocks", tags=["prompt-blocks"])

VALID_KINDS = ("stage", "compliance", "role", "escalation")


class BlockSaveRequest(BaseModel):
    name: str
    content: str
    kind: str = "stage"


async def _get_block(db: AsyncSession, scope: Scope, name: str) -> PromptBlock:
    block = (
        await db.execute(
            select(PromptBlock).where(
                PromptBlock.tenant_id == scope.tenant_id,
                PromptBlock.project_id == scope.project_id,
                PromptBlock.name == name,
            )
        )
    ).scalar_one_or_none()
    if block is None:
        raise HTTPException(status_code=404, detail=f"prompt block '{name}' not found")
    return block


@router.post("")
async def save_block(
    req: BlockSaveRequest,
    scope: Scope = Depends(resolve_scope),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Create the block or append a new draft version to an existing one."""
    if req.kind not in VALID_KINDS:
        raise HTTPException(status_code=422, detail=f"kind must be one of {VALID_KINDS}")
    if not req.content.strip():
        raise HTTPException(status_code=422, detail="content must not be empty")
    block = (
        await db.execute(
            select(PromptBlock).where(
                PromptBlock.tenant_id == scope.tenant_id,
                PromptBlock.project_id == scope.project_id,
                PromptBlock.name == req.name,
            )
        )
    ).scalar_one_or_none()
    if block is None:
        block = PromptBlock(
            tenant_id=scope.tenant_id, project_id=scope.project_id, name=req.name, kind=req.kind
        )
        db.add(block)
        await db.flush()
    block.latest_version += 1
    block.kind = req.kind
    block.updated_at = utcnow()
    db.add(PromptBlockVersion(block_id=block.id, version=block.latest_version, content=req.content))
    await db.commit()
    return {"name": block.name, "version": block.latest_version, "status": "draft"}


@router.get("")
async def list_blocks(scope: Scope = Depends(resolve_scope), db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (
        await db.execute(
            select(PromptBlock, PromptBlockVersion)
            .join(PromptBlockVersion, PromptBlockVersion.block_id == PromptBlock.id)
            .where(
                PromptBlock.tenant_id == scope.tenant_id,
                PromptBlock.project_id == scope.project_id,
                PromptBlockVersion.version == PromptBlock.latest_version,
            )
        )
    ).all()
    return [
        {
            "name": b.name,
            "kind": b.kind,
            "latest_version": b.latest_version,
            "latest_status": v.status,
            "updated_at": b.updated_at.isoformat(),
        }
        for b, v in rows
    ]


@router.get("/{name}")
async def get_block(
    name: str, scope: Scope = Depends(resolve_scope), db: AsyncSession = Depends(get_db)
) -> dict:
    block = await _get_block(db, scope, name)
    versions = (
        (
            await db.execute(
                select(PromptBlockVersion)
                .where(PromptBlockVersion.block_id == block.id)
                .order_by(PromptBlockVersion.version.desc())
            )
        )
        .scalars()
        .all()
    )
    return {
        "name": block.name,
        "kind": block.kind,
        "versions": [
            {"version": v.version, "status": v.status, "content": v.content} for v in versions
        ],
    }


@router.post("/{name}/publish")
async def publish_block(
    name: str, scope: Scope = Depends(resolve_scope), db: AsyncSession = Depends(get_db)
) -> dict:
    block = await _get_block(db, scope, name)
    version = (
        await db.execute(
            select(PromptBlockVersion)
            .where(PromptBlockVersion.block_id == block.id)
            .order_by(PromptBlockVersion.version.desc())
            .limit(1)
        )
    ).scalar_one()
    version.status = "published"
    await db.commit()
    return {"name": block.name, "version": version.version, "status": "published"}


async def resolve_published_blocks(
    db: AsyncSession, scope: Scope, names: set[str]
) -> tuple[dict[str, dict], set[str]]:
    """Resolve block names to their newest PUBLISHED version.
    Returns ({name: {"version": v, "content": c}}, missing_names)."""
    if not names:
        return {}, set()
    rows = (
        await db.execute(
            select(PromptBlock, PromptBlockVersion)
            .join(PromptBlockVersion, PromptBlockVersion.block_id == PromptBlock.id)
            .where(
                PromptBlock.tenant_id == scope.tenant_id,
                PromptBlock.project_id == scope.project_id,
                PromptBlock.name.in_(sorted(names)),
                PromptBlockVersion.status == "published",
            )
            .order_by(PromptBlockVersion.version.asc())
        )
    ).all()
    resolved: dict[str, dict] = {}
    for b, v in rows:  # ascending — the last write per name wins = newest published
        resolved[b.name] = {"version": v.version, "content": v.content}
    return resolved, names - set(resolved)

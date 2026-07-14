"""SOP CRUD with versioning. Publishing runs the linter — structural problems are
publish blockers (the credit-card-SOP lesson from the research).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import Sop, SopVersion, utcnow
from ..schemas import SopMeta, SopSaveRequest, TaskDefinition
from ..sop_graph import SOPGraph
from ..tenancy import Scope, resolve_scope

router = APIRouter(prefix="/sops", tags=["sops"])


async def _get_sop(db: AsyncSession, scope: Scope, sop_id: str) -> Sop:
    sop = (
        await db.execute(
            select(Sop).where(
                Sop.id == sop_id, Sop.tenant_id == scope.tenant_id, Sop.project_id == scope.project_id
            )
        )
    ).scalar_one_or_none()
    if sop is None:
        raise HTTPException(status_code=404, detail="SOP not found")
    return sop


@router.post("", response_model=SopMeta)
async def create_sop(
    req: SopSaveRequest,
    scope: Scope = Depends(resolve_scope),
    db: AsyncSession = Depends(get_db),
) -> SopMeta:
    existing = (
        await db.execute(
            select(Sop).where(
                Sop.tenant_id == scope.tenant_id,
                Sop.project_id == scope.project_id,
                Sop.name == req.definition.name,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"SOP '{req.definition.name}' already exists; PUT to update")
    sop = Sop(
        tenant_id=scope.tenant_id, project_id=scope.project_id, name=req.definition.name, latest_version=1
    )
    db.add(sop)
    await db.flush()
    db.add(SopVersion(sop_id=sop.id, version=1, status="draft", definition=req.definition.model_dump()))
    await db.commit()
    return SopMeta(id=sop.id, name=sop.name, latest_version=1, updated_at=sop.updated_at.isoformat())


@router.get("", response_model=list[SopMeta])
async def list_sops(scope: Scope = Depends(resolve_scope), db: AsyncSession = Depends(get_db)) -> list[SopMeta]:
    rows = (
        await db.execute(
            select(Sop).where(Sop.tenant_id == scope.tenant_id, Sop.project_id == scope.project_id)
        )
    ).scalars().all()
    return [
        SopMeta(id=s.id, name=s.name, latest_version=s.latest_version, updated_at=s.updated_at.isoformat())
        for s in rows
    ]


@router.get("/{sop_id}")
async def get_sop(
    sop_id: str, scope: Scope = Depends(resolve_scope), db: AsyncSession = Depends(get_db)
) -> dict:
    sop = await _get_sop(db, scope, sop_id)
    version = (
        await db.execute(
            select(SopVersion)
            .where(SopVersion.sop_id == sop.id)
            .order_by(SopVersion.version.desc())
            .limit(1)
        )
    ).scalar_one()
    return {
        "id": sop.id,
        "name": sop.name,
        "version": version.version,
        "status": version.status,
        "definition": version.definition,
    }


@router.put("/{sop_id}", response_model=SopMeta)
async def update_sop(
    sop_id: str,
    req: SopSaveRequest,
    scope: Scope = Depends(resolve_scope),
    db: AsyncSession = Depends(get_db),
) -> SopMeta:
    sop = await _get_sop(db, scope, sop_id)
    sop.latest_version += 1
    sop.name = req.definition.name
    sop.updated_at = utcnow()
    db.add(
        SopVersion(
            sop_id=sop.id, version=sop.latest_version, status="draft", definition=req.definition.model_dump()
        )
    )
    await db.commit()
    return SopMeta(
        id=sop.id, name=sop.name, latest_version=sop.latest_version, updated_at=sop.updated_at.isoformat()
    )


@router.post("/{sop_id}/lint")
async def lint_sop(
    sop_id: str, scope: Scope = Depends(resolve_scope), db: AsyncSession = Depends(get_db)
) -> dict:
    sop = await _get_sop(db, scope, sop_id)
    version = (
        await db.execute(
            select(SopVersion).where(SopVersion.sop_id == sop.id).order_by(SopVersion.version.desc()).limit(1)
        )
    ).scalar_one()
    problems = SOPGraph(TaskDefinition.model_validate(version.definition)).lint()
    return {"version": version.version, "problems": problems, "publishable": not problems}


@router.post("/{sop_id}/publish")
async def publish_sop(
    sop_id: str, scope: Scope = Depends(resolve_scope), db: AsyncSession = Depends(get_db)
) -> dict:
    sop = await _get_sop(db, scope, sop_id)
    version = (
        await db.execute(
            select(SopVersion).where(SopVersion.sop_id == sop.id).order_by(SopVersion.version.desc()).limit(1)
        )
    ).scalar_one()
    problems = SOPGraph(TaskDefinition.model_validate(version.definition)).lint()
    if problems:
        raise HTTPException(status_code=422, detail={"message": "lint failed", "problems": problems})
    version.status = "published"
    await db.commit()
    return {"version": version.version, "status": "published"}

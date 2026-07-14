"""Bootstrap/admin routes. Tenant creation is guarded by the deployment's
SOPILOT_ADMIN_TOKEN; project creation is tenant-key-scoped.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_db
from ..models import ApiKey, Project, Tenant
from ..schemas import ProjectCreateRequest, TenantCreateRequest, TenantCreateResponse
from ..tenancy import VALID_SUBSYSTEMS, generate_api_key, resolve_tenant

router = APIRouter(prefix="/admin", tags=["admin"])


def require_admin_token(x_admin_token: str = Header(default="", alias="X-Admin-Token")) -> None:
    expected = get_settings().admin_token
    if not expected or x_admin_token != expected:
        raise HTTPException(status_code=403, detail="invalid admin token")


@router.post("/tenants", response_model=TenantCreateResponse, dependencies=[Depends(require_admin_token)])
async def create_tenant(req: TenantCreateRequest, db: AsyncSession = Depends(get_db)) -> TenantCreateResponse:
    existing = (await db.execute(select(Tenant).where(Tenant.slug == req.slug))).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"tenant '{req.slug}' already exists")
    tenant = Tenant(slug=req.slug, name=req.name or req.slug)
    raw_key, key_hash = generate_api_key()
    db.add(tenant)
    await db.flush()
    db.add(ApiKey(tenant_id=tenant.id, key_hash=key_hash, label="bootstrap", role="admin"))
    await db.commit()
    return TenantCreateResponse(tenant_id=tenant.id, slug=tenant.slug, api_key=raw_key)


@router.post("/projects")
async def create_project(
    req: ProjectCreateRequest,
    tenant: Tenant = Depends(resolve_tenant),
    db: AsyncSession = Depends(get_db),
) -> dict:
    existing = (
        await db.execute(select(Project).where(Project.tenant_id == tenant.id, Project.slug == req.slug))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"project '{req.slug}' already exists")
    if req.subsystems and req.subsystems not in VALID_SUBSYSTEMS:
        raise HTTPException(status_code=422, detail=f"subsystems must be one of {VALID_SUBSYSTEMS}")
    project = Project(
        tenant_id=tenant.id, slug=req.slug, name=req.name or req.slug, subsystems=req.subsystems
    )
    db.add(project)
    await db.commit()
    return {"project_id": project.id, "slug": project.slug, "subsystems": project.subsystems or "default"}


@router.get("/projects")
async def list_projects(tenant: Tenant = Depends(resolve_tenant), db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (await db.execute(select(Project).where(Project.tenant_id == tenant.id))).scalars().all()
    return [
        {"project_id": p.id, "slug": p.slug, "name": p.name, "subsystems": p.subsystems or "default"}
        for p in rows
    ]

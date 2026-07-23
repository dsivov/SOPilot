"""Bootstrap/admin routes. Tenant creation is guarded by the deployment's
SOPILOT_ADMIN_TOKEN; project creation is tenant-key-scoped.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pydantic import BaseModel

from ..config import get_settings
from ..db import get_db
from ..models import (
    ABTest,
    ApiKey,
    Connector,
    ConversationSession,
    Corpus,
    DataFetchAudit,
    PoolPickAudit,
    PrecedentTrace,
    Project,
    PromptBlock,
    RoutingEvent,
    Tenant,
    utcnow,
)
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


class ProjectUpdateRequest(BaseModel):
    subsystems: str


@router.patch("/projects/{slug}")
async def update_project(
    slug: str,
    req: ProjectUpdateRequest,
    tenant: Tenant = Depends(resolve_tenant),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if req.subsystems not in ("", *VALID_SUBSYSTEMS):
        raise HTTPException(status_code=422, detail=f"subsystems must be one of {VALID_SUBSYSTEMS} or ''")
    project = (
        await db.execute(select(Project).where(Project.tenant_id == tenant.id, Project.slug == slug))
    ).scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail=f"project '{slug}' not found")
    project.subsystems = req.subsystems
    await db.commit()
    return {"slug": slug, "subsystems": project.subsystems or "default"}


@router.get("/whoami")
async def whoami(tenant: Tenant = Depends(resolve_tenant)) -> dict:
    return {"tenant_slug": tenant.slug, "tenant_name": tenant.name}


@router.get("/projects")
async def list_projects(tenant: Tenant = Depends(resolve_tenant), db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (await db.execute(select(Project).where(Project.tenant_id == tenant.id))).scalars().all()
    return [
        {"project_id": p.id, "slug": p.slug, "name": p.name, "subsystems": p.subsystems or "default"}
        for p in rows
    ]


# ---------- Platform admin: tenant & API-key management (admin-token guarded) ----------
#
# The RBAC management surface for the admin console. Tenants and their sop_ keys
# are administered here; the raw key is only ever returned at mint time (only its
# sha256 is stored), so "show it to the tenant owner" happens exactly once.

# Tenant-scoped tables with a plain tenant_id (no FK to tenants): bulk-delete these
# by tenant_id on tenant deletion. Their children (turns, corpus_docs, block/sop
# versions) cascade via their own FKs; projects / api_keys / tenant_secrets / sops
# cascade from the tenant row itself (ondelete=CASCADE).
_TENANT_SCOPED_PARENTS = [
    Connector, ABTest, PromptBlock, ConversationSession,
    RoutingEvent, PrecedentTrace, Corpus, DataFetchAudit, PoolPickAudit,
]


@router.get("/tenants", dependencies=[Depends(require_admin_token)])
async def list_tenants(db: AsyncSession = Depends(get_db)) -> list[dict]:
    tenants = (await db.execute(select(Tenant).order_by(Tenant.created_at))).scalars().all()
    proj_counts = dict((await db.execute(
        select(Project.tenant_id, func.count()).group_by(Project.tenant_id))).all())
    key_counts = dict((await db.execute(
        select(ApiKey.tenant_id, func.count()).where(ApiKey.revoked_at.is_(None)).group_by(ApiKey.tenant_id))).all())
    return [
        {
            "tenant_id": t.id, "slug": t.slug, "name": t.name,
            "created_at": t.created_at.isoformat(),
            "projects": proj_counts.get(t.id, 0),
            "active_keys": key_counts.get(t.id, 0),
        }
        for t in tenants
    ]


@router.delete("/tenants/{slug}", dependencies=[Depends(require_admin_token)])
async def delete_tenant(slug: str, db: AsyncSession = Depends(get_db)) -> dict:
    tenant = (await db.execute(select(Tenant).where(Tenant.slug == slug))).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status_code=404, detail=f"tenant '{slug}' not found")
    for model in _TENANT_SCOPED_PARENTS:
        await db.execute(delete(model).where(model.tenant_id == tenant.id))
    await db.delete(tenant)  # DB cascades projects / api_keys / tenant_secrets / sops
    await db.commit()
    return {"deleted": slug}


class KeyCreateRequest(BaseModel):
    label: str = ""
    role: str = "runtime"  # runtime | admin


@router.get("/tenants/{slug}/keys", dependencies=[Depends(require_admin_token)])
async def list_keys(slug: str, db: AsyncSession = Depends(get_db)) -> list[dict]:
    tenant = (await db.execute(select(Tenant).where(Tenant.slug == slug))).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status_code=404, detail=f"tenant '{slug}' not found")
    keys = (await db.execute(
        select(ApiKey).where(ApiKey.tenant_id == tenant.id).order_by(ApiKey.created_at))).scalars().all()
    return [
        {
            "id": k.id, "label": k.label or "(unlabeled)", "role": k.role,
            "hash_prefix": k.key_hash[:10],  # non-secret — lets the owner tell keys apart
            "created_at": k.created_at.isoformat(),
            "revoked": k.revoked_at is not None,
            "revoked_at": k.revoked_at.isoformat() if k.revoked_at else None,
        }
        for k in keys
    ]


@router.post("/tenants/{slug}/keys", dependencies=[Depends(require_admin_token)])
async def issue_key(slug: str, req: KeyCreateRequest, db: AsyncSession = Depends(get_db)) -> dict:
    """Mint a new sop_ key for a tenant and return it EXACTLY ONCE (only the sha256
    is stored). Hand the raw key to the tenant owner now — it cannot be shown again."""
    if req.role not in ("runtime", "admin"):
        raise HTTPException(status_code=422, detail="role must be 'runtime' or 'admin'")
    tenant = (await db.execute(select(Tenant).where(Tenant.slug == slug))).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status_code=404, detail=f"tenant '{slug}' not found")
    raw_key, key_hash = generate_api_key()
    key = ApiKey(tenant_id=tenant.id, key_hash=key_hash, label=req.label.strip()[:200], role=req.role)
    db.add(key)
    await db.commit()
    return {"key_id": key.id, "label": key.label, "role": key.role, "api_key": raw_key}


# ---- per-project export/import from the admin console (same engine as the
# tenant-scoped /project/export|import routes; identity comes from the path) ----

async def _admin_scope(db: AsyncSession, slug: str, project_slug: str, create: bool = False,
                       project_meta: dict | None = None):
    """Resolve tenant+project slugs to a Scope. With create=True (import), a
    missing tenant/project is created on the fly — importing a bundle into a
    fresh deployment needs no prior setup (mint keys from the console after)."""
    from ..tenancy import Scope
    tenant = (await db.execute(select(Tenant).where(Tenant.slug == slug))).scalar_one_or_none()
    if tenant is None:
        if not create:
            raise HTTPException(status_code=404, detail=f"tenant '{slug}' not found")
        tenant = Tenant(slug=slug, name=slug)
        db.add(tenant)
        await db.flush()
    project = (await db.execute(select(Project).where(
        Project.tenant_id == tenant.id, Project.slug == project_slug))).scalar_one_or_none()
    if project is None:
        if not create:
            raise HTTPException(status_code=404, detail=f"project '{project_slug}' not found")
        meta = project_meta or {}
        subsystems = meta.get("subsystems")
        project = Project(
            tenant_id=tenant.id, slug=project_slug, name=str(meta.get("name") or project_slug),
            subsystems=subsystems if subsystems in VALID_SUBSYSTEMS else None,
        )
        db.add(project)
        await db.flush()
    return Scope(tenant_id=tenant.id, project_id=project.id)


@router.get("/tenants/{slug}/projects", dependencies=[Depends(require_admin_token)])
async def admin_list_projects(slug: str, db: AsyncSession = Depends(get_db)) -> list[dict]:
    tenant = (await db.execute(select(Tenant).where(Tenant.slug == slug))).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status_code=404, detail=f"tenant '{slug}' not found")
    rows = (await db.execute(select(Project).where(Project.tenant_id == tenant.id).order_by(Project.slug))).scalars().all()
    return [{"slug": p.slug, "name": p.name, "subsystems": p.subsystems or "default"} for p in rows]


@router.get("/tenants/{slug}/projects/{project_slug}/export", dependencies=[Depends(require_admin_token)])
async def admin_export_project(slug: str, project_slug: str, db: AsyncSession = Depends(get_db)) -> dict:
    from .project_io import export_scope
    return await export_scope(db, await _admin_scope(db, slug, project_slug))


@router.post("/tenants/{slug}/projects/{project_slug}/import", dependencies=[Depends(require_admin_token)])
async def admin_import_project(slug: str, project_slug: str, bundle: dict, db: AsyncSession = Depends(get_db)) -> dict:
    """Import a bundle; the tenant and project are created if they don't exist
    (project name/subsystems seeded from the bundle's project block)."""
    from .project_io import ImportBundle, import_scope
    scope = await _admin_scope(db, slug, project_slug, create=True,
                               project_meta=bundle.get("project") if isinstance(bundle.get("project"), dict) else None)
    return await import_scope(db, scope, ImportBundle.model_validate(bundle))


@router.post("/tenants/{slug}/login-key", dependencies=[Depends(require_admin_token)])
async def issue_login_key(slug: str, db: AsyncSession = Depends(get_db)) -> dict:
    """One-click console login: mint a fresh admin-role key for the tenant (raw
    keys are never stored, so an existing key can't be reused) and revoke the
    previous console-login key so they don't accumulate. The key is handed
    straight to the Studio's stored creds — never displayed."""
    tenant = (await db.execute(select(Tenant).where(Tenant.slug == slug))).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status_code=404, detail=f"tenant '{slug}' not found")
    # Ephemeral machine-minted keys: hard-delete stale ones (revoked or not)
    # rather than striking through — they'd otherwise pile up in the key list.
    await db.execute(delete(ApiKey).where(ApiKey.tenant_id == tenant.id, ApiKey.label == "console-login"))
    raw_key, key_hash = generate_api_key()
    db.add(ApiKey(tenant_id=tenant.id, key_hash=key_hash, label="console-login", role="admin"))
    projects = (await db.execute(
        select(Project.slug).where(Project.tenant_id == tenant.id).order_by(Project.slug))).scalars().all()
    await db.commit()
    return {"api_key": raw_key, "projects": list(projects)}


@router.post("/tenants/{slug}/keys/{key_id}/revoke", dependencies=[Depends(require_admin_token)])
async def revoke_key(slug: str, key_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    tenant = (await db.execute(select(Tenant).where(Tenant.slug == slug))).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status_code=404, detail=f"tenant '{slug}' not found")
    key = (await db.execute(
        select(ApiKey).where(ApiKey.id == key_id, ApiKey.tenant_id == tenant.id))).scalar_one_or_none()
    if key is None:
        raise HTTPException(status_code=404, detail="key not found")
    if key.revoked_at is None:
        key.revoked_at = utcnow()
        await db.commit()
    return {"key_id": key.id, "revoked": True}

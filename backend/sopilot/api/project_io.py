"""Project export / import: the full authored configuration of a project —
SOPs, prompt blocks, connectors — as one portable JSON bundle.

Export takes the LATEST version of each SOP / block with its status; import
upserts by name (create, or append a new version), re-publishing what the
bundle marks published — SOP publishing goes through the normal lint + block
gate, and failures downgrade to draft with a warning rather than aborting the
whole import. Connector secrets are NOT part of the bundle (they live in
tenant_secrets and never leave the deployment).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..connectors import CONNECTOR_KINDS
from ..db import get_db
from ..models import Connector, Project, PromptBlock, PromptBlockVersion, Sop, SopVersion, utcnow
from ..schemas import TaskDefinition
from ..sop_graph import SOPGraph
from ..tenancy import Scope, resolve_scope

router = APIRouter(prefix="/project", tags=["project"])

EXPORT_KIND = "sopilot-project-export"
EXPORT_VERSION = 1


async def export_scope(db: AsyncSession, scope: Scope) -> dict:
    """Build the export bundle for a scope. Shared by the tenant-key route and
    the admin-console (admin-token) route."""
    project = (await db.execute(select(Project).where(Project.id == scope.project_id))).scalar_one()

    sops = []
    for sop in (await db.execute(select(Sop).where(
            Sop.tenant_id == scope.tenant_id, Sop.project_id == scope.project_id).order_by(Sop.name))).scalars():
        v = (await db.execute(select(SopVersion).where(SopVersion.sop_id == sop.id)
                              .order_by(SopVersion.version.desc()).limit(1))).scalar_one_or_none()
        if v is not None:
            sops.append({"name": sop.name, "status": v.status, "definition": v.definition})

    blocks = [
        {"name": b.name, "kind": b.kind, "status": v.status, "content": v.content}
        for b, v in (await db.execute(
            select(PromptBlock, PromptBlockVersion)
            .join(PromptBlockVersion, PromptBlockVersion.block_id == PromptBlock.id)
            .where(
                PromptBlock.tenant_id == scope.tenant_id,
                PromptBlock.project_id == scope.project_id,
                PromptBlockVersion.version == PromptBlock.latest_version,
            ).order_by(PromptBlock.name))).all()
    ]

    connectors = [
        {"name": c.name, "kind": c.kind, "description": c.description, "config": c.config, "enabled": c.enabled}
        for c in (await db.execute(select(Connector).where(
            Connector.tenant_id == scope.tenant_id, Connector.project_id == scope.project_id)
            .order_by(Connector.name))).scalars()
    ]

    return {
        "kind": EXPORT_KIND,
        "export_version": EXPORT_VERSION,
        "exported_at": utcnow().isoformat(),
        "project": {"slug": project.slug, "name": project.name, "subsystems": project.subsystems or "default"},
        "sops": sops,
        "prompt_blocks": blocks,
        "connectors": connectors,
    }


class ImportBundle(BaseModel):
    kind: str = ""
    export_version: int = 0
    sops: list[dict] = Field(default_factory=list)
    prompt_blocks: list[dict] = Field(default_factory=list)
    connectors: list[dict] = Field(default_factory=list)


async def import_scope(db: AsyncSession, scope: Scope, bundle: ImportBundle) -> dict:
    """Upsert the bundle into the scope's project (names are the identity —
    existing items get a new version, missing ones are created). Prompt blocks
    land first so published SOPs can pass the block gate."""
    if bundle.kind != EXPORT_KIND:
        raise HTTPException(status_code=422, detail=f"not a {EXPORT_KIND} bundle")
    if bundle.export_version > EXPORT_VERSION:
        raise HTTPException(status_code=422, detail=f"bundle version {bundle.export_version} is newer than this server supports")
    summary: dict = {k: {"created": 0, "updated": 0, "published": 0} for k in ("sops", "prompt_blocks", "connectors")}
    warnings: list[str] = []

    # ---- prompt blocks (first: SOP publishing depends on them) ----
    for item in bundle.prompt_blocks:
        name, content = str(item.get("name", "")).strip(), str(item.get("content", ""))
        if not name or not content.strip():
            warnings.append(f"prompt block skipped (missing name/content): {name or '?'}")
            continue
        block = (await db.execute(select(PromptBlock).where(
            PromptBlock.tenant_id == scope.tenant_id, PromptBlock.project_id == scope.project_id,
            PromptBlock.name == name))).scalar_one_or_none()
        if block is None:
            block = PromptBlock(tenant_id=scope.tenant_id, project_id=scope.project_id,
                                name=name, kind=str(item.get("kind", "stage")))
            db.add(block)
            await db.flush()
            summary["prompt_blocks"]["created"] += 1
        else:
            summary["prompt_blocks"]["updated"] += 1
        block.latest_version += 1
        block.updated_at = utcnow()
        status = "published" if item.get("status") == "published" else "draft"
        db.add(PromptBlockVersion(block_id=block.id, version=block.latest_version, content=content, status=status))
        if status == "published":
            summary["prompt_blocks"]["published"] += 1
    await db.flush()

    # ---- SOPs ----
    from ..runtime import collect_prompt_block_names
    from .prompt_blocks import resolve_published_blocks

    for item in bundle.sops:
        name = str(item.get("name", "")).strip()
        try:
            task_def = TaskDefinition.model_validate(item.get("definition") or {})
        except Exception as e:  # noqa: BLE001 — a bad definition skips one SOP, not the import
            warnings.append(f"SOP '{name or '?'}' skipped: invalid definition ({str(e)[:150]})")
            continue
        name = name or task_def.name
        sop = (await db.execute(select(Sop).where(
            Sop.tenant_id == scope.tenant_id, Sop.project_id == scope.project_id,
            Sop.name == name))).scalar_one_or_none()
        if sop is None:
            sop = Sop(tenant_id=scope.tenant_id, project_id=scope.project_id, name=name, latest_version=0)
            db.add(sop)
            await db.flush()
            summary["sops"]["created"] += 1
        else:
            summary["sops"]["updated"] += 1
        sop.latest_version += 1
        sop.updated_at = utcnow()
        version = SopVersion(sop_id=sop.id, version=sop.latest_version, status="draft",
                             definition=task_def.model_dump())
        db.add(version)
        if item.get("status") == "published":
            # the normal publish gate: lint + every bound block published
            problems = SOPGraph(task_def).lint()
            _, missing = await resolve_published_blocks(db, scope, collect_prompt_block_names(task_def))
            problems += [f"prompt block '{b}' has no published version" for b in sorted(missing)]
            if problems:
                warnings.append(f"SOP '{name}' imported as draft — publish gate failed: {'; '.join(problems[:3])}")
            else:
                version.status = "published"
                summary["sops"]["published"] += 1

    # ---- connectors ----
    for item in bundle.connectors:
        name, kind = str(item.get("name", "")).strip(), str(item.get("kind", ""))
        if not name or kind not in CONNECTOR_KINDS:
            warnings.append(f"connector skipped (missing name or bad kind): {name or '?'} ({kind})")
            continue
        conn = (await db.execute(select(Connector).where(
            Connector.tenant_id == scope.tenant_id, Connector.project_id == scope.project_id,
            Connector.name == name))).scalar_one_or_none()
        if conn is None:
            conn = Connector(tenant_id=scope.tenant_id, project_id=scope.project_id, name=name, kind=kind)
            db.add(conn)
            summary["connectors"]["created"] += 1
        else:
            summary["connectors"]["updated"] += 1
        conn.kind = kind
        conn.description = str(item.get("description", ""))[:500]
        conn.config = item.get("config") or {}
        conn.enabled = bool(item.get("enabled", True))
        conn.updated_at = utcnow()

    await db.commit()
    return {"summary": summary, "warnings": warnings}


@router.get("/export")
async def export_project(scope: Scope = Depends(resolve_scope), db: AsyncSession = Depends(get_db)) -> dict:
    return await export_scope(db, scope)


@router.post("/import")
async def import_project(
    bundle: ImportBundle, scope: Scope = Depends(resolve_scope), db: AsyncSession = Depends(get_db)
) -> dict:
    return await import_scope(db, scope, bundle)

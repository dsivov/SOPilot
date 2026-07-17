"""Tenant→project scoping and API-key auth.

A raw API key looks like ``sop_<40 hex chars>``; only its sha256 lands in the DB.
Every request resolves to a Scope; every scoped query and every Redis key goes
through it. Isolation lives here, not in per-route if-statements.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_db
from .models import ApiKey, Project, Tenant


VALID_SUBSYSTEMS = ("sop", "retrieval", "both", "advisory")


@dataclass(frozen=True)
class Scope:
    tenant_id: str
    project_id: str
    # D-9 subsystem mode for this project: "sop" | "retrieval" | "both".
    subsystems: str = "both"

    def redis_prefix(self) -> str:
        return f"sop:{self.tenant_id}:{self.project_id}"

    @property
    def sop_enabled(self) -> bool:
        return self.subsystems in ("sop", "both", "advisory")

    @property
    def retrieval_enabled(self) -> bool:
        return self.subsystems in ("retrieval", "both", "advisory")

    @property
    def advisory(self) -> bool:
        """D-13: SOP + fresh data go straight to the responder; classification,
        tracking, traces and prefetch run OFF the reply path. For knowledge-
        delivery workloads; gated mode remains for compliance-critical flows."""
        return self.subsystems == "advisory"


def generate_api_key() -> tuple[str, str]:
    """Returns (raw_key, sha256_hash). The raw key is shown exactly once."""
    raw = "sop_" + secrets.token_hex(20)
    return raw, hash_api_key(raw)


def hash_api_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def resolve_tenant(
    authorization: str = Header(default=""),
    db: AsyncSession = Depends(get_db),
) -> Tenant:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer API key")
    key_hash = hash_api_key(authorization.removeprefix("Bearer ").strip())
    row = (
        await db.execute(
            select(ApiKey, Tenant)
            .join(Tenant, Tenant.id == ApiKey.tenant_id)
            .where(ApiKey.key_hash == key_hash, ApiKey.revoked_at.is_(None))
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=401, detail="invalid API key")
    return row[1]


async def resolve_scope(
    x_project: str = Header(default="", alias="X-Project"),
    tenant: Tenant = Depends(resolve_tenant),
    db: AsyncSession = Depends(get_db),
) -> Scope:
    if not x_project:
        raise HTTPException(status_code=400, detail="X-Project header required")
    project = (
        await db.execute(
            select(Project).where(Project.tenant_id == tenant.id, Project.slug == x_project)
        )
    ).scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail=f"project '{x_project}' not found in tenant")
    from .config import get_settings

    subsystems = project.subsystems or get_settings().subsystems
    if subsystems not in VALID_SUBSYSTEMS:
        subsystems = "both"
    return Scope(tenant_id=tenant.id, project_id=project.id, subsystems=subsystems)

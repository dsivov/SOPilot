"""Tenant connector secrets — write-only API (reads return names, never values)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import Tenant, TenantSecret, utcnow
from ..secrets import encrypt, using_dev_key
from ..tenancy import resolve_tenant

router = APIRouter(prefix="/secrets", tags=["secrets"])


class SecretPutRequest(BaseModel):
    name: str
    value: str


@router.put("")
async def put_secret(
    req: SecretPutRequest,
    tenant: Tenant = Depends(resolve_tenant),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if not req.name.strip() or not req.value:
        raise HTTPException(status_code=422, detail="name and value are required")
    row = (
        await db.execute(
            select(TenantSecret).where(TenantSecret.tenant_id == tenant.id, TenantSecret.name == req.name)
        )
    ).scalar_one_or_none()
    if row is None:
        row = TenantSecret(tenant_id=tenant.id, name=req.name.strip(), value_encrypted=encrypt(req.value))
        db.add(row)
    else:
        row.value_encrypted = encrypt(req.value)
        row.updated_at = utcnow()
    await db.commit()
    return {"name": req.name, "stored": True, "dev_key_warning": using_dev_key()}


@router.get("")
async def list_secrets(
    tenant: Tenant = Depends(resolve_tenant), db: AsyncSession = Depends(get_db)
) -> list[dict]:
    rows = (
        (await db.execute(select(TenantSecret).where(TenantSecret.tenant_id == tenant.id)))
        .scalars()
        .all()
    )
    return [{"name": r.name, "updated_at": r.updated_at.isoformat()} for r in rows]


@router.delete("/{name}")
async def delete_secret(
    name: str, tenant: Tenant = Depends(resolve_tenant), db: AsyncSession = Depends(get_db)
) -> dict:
    row = (
        await db.execute(
            select(TenantSecret).where(TenantSecret.tenant_id == tenant.id, TenantSecret.name == name)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"secret '{name}' not found")
    await db.delete(row)
    await db.commit()
    return {"deleted": name}

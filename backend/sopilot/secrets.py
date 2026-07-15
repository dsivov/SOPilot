"""Tenant connector secrets — encrypted at rest with the deployment key.

A tenant's CRM/API credentials are tenant data: scoped like everything else and
never returned by the API after write (names only). Encryption is Fernet keyed
from SOPILOT_SECRET_KEY; rotating that key requires re-writing stored secrets.
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .models import TenantSecret

_DEV_FALLBACK = "sopilot-dev-only-secret-key"


def _fernet() -> Fernet:
    raw = get_settings().secret_key or _DEV_FALLBACK
    digest = hashlib.sha256(raw.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()


def using_dev_key() -> bool:
    return not get_settings().secret_key


async def get_secret(db: AsyncSession, tenant_id: str, name: str) -> str | None:
    row = (
        await db.execute(
            select(TenantSecret).where(TenantSecret.tenant_id == tenant_id, TenantSecret.name == name)
        )
    ).scalar_one_or_none()
    return decrypt(row.value_encrypted) if row else None

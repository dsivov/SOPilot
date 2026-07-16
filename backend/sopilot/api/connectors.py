"""Connector registry (D-10): configure, monitor, and live-test the retrieval
systems behind background prefetch — MCP servers, RAG/HTTP endpoints, managed
corpora. Connection details live here at project level; SOP stages bind by
name via `data_dependencies[].config.connector`."""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..connectors import CONNECTOR_KINDS
from ..db import get_db
from ..models import Connector, DataFetchAudit, Sop, SopVersion, utcnow
from ..schemas import DataDependency
from ..tenancy import Scope, resolve_scope

router = APIRouter(prefix="/connectors", tags=["connectors"])


class ConnectorSaveRequest(BaseModel):
    kind: str
    description: str = ""
    config: dict = Field(default_factory=dict)
    enabled: bool = True


class ConnectorTestRequest(BaseModel):
    query: str = "connectivity test — say hello"


async def _get(db: AsyncSession, scope: Scope, name: str) -> Connector:
    row = (
        await db.execute(
            select(Connector).where(
                Connector.tenant_id == scope.tenant_id,
                Connector.project_id == scope.project_id,
                Connector.name == name,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"connector '{name}' not found")
    return row


@router.get("")
async def list_connectors(
    scope: Scope = Depends(resolve_scope), db: AsyncSession = Depends(get_db), days: int = 7
) -> list[dict]:
    """Registry + health: fetch volume, error rate and latency percentiles from
    the audit trail, plus how many SOPs bind each connector."""
    from datetime import timedelta

    rows = (
        (
            await db.execute(
                select(Connector)
                .where(Connector.tenant_id == scope.tenant_id, Connector.project_id == scope.project_id)
                .order_by(Connector.name)
            )
        )
        .scalars()
        .all()
    )

    since = utcnow() - timedelta(days=days)
    stats = {
        r[0]: {
            "fetches": int(r[1]),
            "errors": int(r[2]),
            "consumed": int(r[3]),
            "p50_ms": int(r[4] or 0),
            "p95_ms": int(r[5] or 0),
            "last_used": r[6].isoformat() if r[6] else None,
        }
        for r in (
            await db.execute(
                select(
                    DataFetchAudit.connector,
                    func.count(),
                    func.sum(case((DataFetchAudit.fetch_error.isnot(None), 1), else_=0)),
                    func.sum(case((DataFetchAudit.consumed.is_(True), 1), else_=0)),
                    func.percentile_cont(0.5).within_group(DataFetchAudit.fetch_duration_ms),
                    func.percentile_cont(0.95).within_group(DataFetchAudit.fetch_duration_ms),
                    func.max(DataFetchAudit.created_at),
                )
                .where(
                    DataFetchAudit.tenant_id == scope.tenant_id,
                    DataFetchAudit.project_id == scope.project_id,
                    DataFetchAudit.connector != "",
                    DataFetchAudit.created_at >= since,
                )
                .group_by(DataFetchAudit.connector)
            )
        ).all()
    }

    # which published SOPs bind each connector (stage-level references)
    refs: dict[str, int] = {}
    sop_rows = (
        await db.execute(
            select(SopVersion.definition)
            .join(Sop, Sop.id == SopVersion.sop_id)
            .where(
                Sop.tenant_id == scope.tenant_id,
                Sop.project_id == scope.project_id,
                SopVersion.version == Sop.latest_version,
            )
        )
    ).all()
    for (definition,) in sop_rows:
        for dep in (definition or {}).get("data_dependencies", []):
            name = (dep.get("config") or {}).get("connector")
            if name:
                refs[name] = refs.get(name, 0) + 1

    return [
        {
            "name": c.name,
            "kind": c.kind,
            "description": c.description,
            "config": c.config,
            "enabled": c.enabled,
            "updated_at": c.updated_at.isoformat(),
            "sop_references": refs.get(c.name, 0),
            "stats_window_days": days,
            "stats": stats.get(c.name, {"fetches": 0, "errors": 0, "consumed": 0, "p50_ms": 0, "p95_ms": 0, "last_used": None}),
        }
        for c in rows
    ]


@router.put("/{name}")
async def save_connector(
    name: str,
    req: ConnectorSaveRequest,
    scope: Scope = Depends(resolve_scope),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if req.kind not in CONNECTOR_KINDS:
        raise HTTPException(status_code=422, detail=f"kind must be one of {CONNECTOR_KINDS}")
    if "connector" in req.config:
        raise HTTPException(status_code=422, detail="a connector's config cannot reference another connector")
    row = (
        await db.execute(
            select(Connector).where(
                Connector.tenant_id == scope.tenant_id,
                Connector.project_id == scope.project_id,
                Connector.name == name,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = Connector(tenant_id=scope.tenant_id, project_id=scope.project_id, name=name, kind=req.kind)
        db.add(row)
    row.kind = req.kind
    row.description = req.description
    row.config = req.config
    row.enabled = req.enabled
    row.updated_at = utcnow()
    await db.commit()
    return {"name": name, "kind": row.kind, "enabled": row.enabled}


@router.delete("/{name}")
async def delete_connector(
    name: str, scope: Scope = Depends(resolve_scope), db: AsyncSession = Depends(get_db)
) -> dict:
    row = await _get(db, scope, name)
    await db.delete(row)
    await db.commit()
    return {"deleted": name}


@router.post("/{name}/test")
async def test_connector(
    name: str,
    req: ConnectorTestRequest,
    scope: Scope = Depends(resolve_scope),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Fire ONE live fetch through the real fetcher with a synthetic dependency.
    Nothing pools, nothing audits — this is the operator's connectivity probe."""
    row = await _get(db, scope, name)
    from ..fetchers.base import get_fetcher

    dep = DataDependency(
        name=f"__test_{name}",
        kind=row.kind if row.kind in ("mock", "rag", "mcp", "http") else "mock",
        config=row.config,
    )
    t0 = time.perf_counter()
    try:
        fetcher = get_fetcher(dep.kind)
        outcome = await fetcher.fetch(
            dep, scope=scope, session_id="connector-test", action_name="__connector_test", query=req.query
        )
        ms = int((time.perf_counter() - t0) * 1000)
        payload_excerpt = str(outcome.payload)[:1500] if outcome.payload is not None else None
        ok = outcome.payload is not None and not outcome.summary.startswith("<")
        return {"ok": ok, "latency_ms": ms, "summary": outcome.summary, "payload_excerpt": payload_excerpt}
    except Exception as e:  # noqa: BLE001 — the probe's job is to show the failure
        return {
            "ok": False,
            "latency_ms": int((time.perf_counter() - t0) * 1000),
            "summary": "",
            "error": f"{type(e).__name__}: {e}",
        }

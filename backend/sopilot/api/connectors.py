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


# ---- discovery: given a base URL, find its API surface (MCP tools or OpenAPI) ----

_URL_RE = None


def _extract_urls(*texts: str) -> list[str]:
    """Pull http(s) URLs out of free text (most recent first)."""
    import re
    global _URL_RE
    if _URL_RE is None:
        _URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+")
    seen: list[str] = []
    for t in texts:
        for m in _URL_RE.findall(t or ""):
            u = m.rstrip(".,;")
            if u not in seen:
                seen.append(u)
    return seen


async def _probe_mcp(url: str) -> dict | None:
    """Try to treat `url` (and url/mcp) as an MCP server; return its tools or None."""
    from fastmcp import Client
    import asyncio as _a
    candidates, base = [], url.rstrip("/")
    candidates.append(url)
    if not base.endswith("/mcp"):
        candidates.append(base + "/mcp")
    for cand in dict.fromkeys(candidates):
        try:
            async with Client(cand) as client:
                tools = await _a.wait_for(client.list_tools(), timeout=12)
            items = []
            for t in tools[:60]:
                schema = getattr(t, "inputSchema", None) or {}
                props = list((schema.get("properties") or {}).keys())[:12] if isinstance(schema, dict) else []
                items.append({"name": t.name, "description": (getattr(t, "description", "") or "")[:300], "args": props})
            if items:
                return {"kind": "mcp", "base_url": cand, "count": len(items), "items": items}
        except Exception:
            continue
    return None


async def _probe_openapi(url: str) -> dict | None:
    """Try to fetch an OpenAPI/FastAPI spec near `url`; return its endpoints or None."""
    import httpx
    from urllib.parse import urlparse
    base = url.rstrip("/")
    p = urlparse(url)
    origin = f"{p.scheme}://{p.netloc}"
    candidates = dict.fromkeys([
        url if url.endswith((".json",)) else None,
        base + "/openapi.json",
        origin + "/openapi.json",
        base + "/docs/openapi.json",
    ])
    async with httpx.AsyncClient(timeout=12, follow_redirects=True) as hc:
        for cand in [c for c in candidates if c]:
            try:
                r = await hc.get(cand)
                if r.status_code != 200:
                    continue
                spec = r.json()
            except Exception:
                continue
            if not isinstance(spec, dict) or "paths" not in spec:
                continue
            info = spec.get("info") or {}
            # spec-declared server base, else the origin we probed
            api_base = origin
            servers = spec.get("servers") or []
            if servers and isinstance(servers[0], dict) and servers[0].get("url"):
                sv = servers[0]["url"]
                api_base = sv if sv.startswith("http") else origin + "/" + sv.lstrip("/")
            items = []
            for path, ops in (spec.get("paths") or {}).items():
                if not isinstance(ops, dict):
                    continue
                for method, op in ops.items():
                    if method.lower() not in ("get", "post", "put", "delete", "patch") or not isinstance(op, dict):
                        continue
                    items.append({
                        "method": method.upper(), "path": path,
                        "summary": (op.get("summary") or op.get("operationId") or "")[:160],
                        "description": (op.get("description") or "")[:200],
                    })
                    if len(items) >= 80:
                        break
                if len(items) >= 80:
                    break
            if items:
                return {"kind": "openapi", "base_url": api_base.rstrip("/"),
                        "title": info.get("title", ""), "count": len(items), "items": items}
    return None


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

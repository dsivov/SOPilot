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


class ConnectorSuggestRequest(BaseModel):
    instruction: str = ""              # e.g. "http://host:9621 — I need current weather by city"
    history: list[dict] = []           # prior chat turns [{role, content}] — conversational memory


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


_SUGGEST_SYS = (
    "You help an operator configure a DATA CONNECTOR for a voice-agent platform. You are given (a) a discovered API "
    "surface — either MCP tools or OpenAPI/FastAPI endpoints found at a URL — and (b) the functionality the operator "
    "wants. Pick the SINGLE most relevant tool/endpoint for that functionality and propose a connector config. If "
    "several fit, pick the best and mention alternatives in the reply. NEVER invent a tool, endpoint, or path that is "
    "not in the discovered list.\n\n"
    "Connector kinds and their config shape:\n"
    "  MCP  → kind:\"mcp\",  config:{ \"server\":<the base_url>, \"tool\":<discovered tool name>, "
    "\"query_arg\":<the tool arg that takes the search/query text, if any>, \"auth_header\":<e.g. X-API-Key, only if "
    "auth is needed>, \"auth_secret\":<a snake_case NAME for the tenant secret to hold the key, only if auth is needed> }\n"
    "  HTTP → kind:\"http\", config:{ \"url\":<base_url + the chosen path>, \"method\":<GET|POST>, "
    "\"query_field\":<the request field/param that receives the query text, if any>, \"result_path\":<dot-path into the "
    "response JSON to the useful payload, if obvious>, \"auth_header\":<only if needed>, \"auth_secret\":<secret NAME, "
    "only if needed> }\n"
    "Never put a real credential in the config — only the secret NAME the operator will create. Suggest a short "
    "snake_case connector name.\n\n"
    "Return ONLY JSON: {\"reply\":\"<a short, helpful explanation of what you picked and why, and any auth the operator "
    "must set up>\", \"suggestion\":{ \"kind\":..., \"name\":..., \"description\":..., \"config\":{...} }, "
    "\"relevant\":[ {\"id\":<the exact discovered tool name, or \"METHOD /path\">, \"why\":<one short phrase>} ] }. "
    "In \"relevant\", list EVERY discovered item that could plausibly serve the request, most relevant first (up to 6), "
    "so the operator can compare — even ones you didn't pick as the primary suggestion. If nothing in the surface fits, "
    "return the reply explaining that, an empty \"relevant\", and omit \"suggestion\" (or set it null)."
)


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


@router.post("/suggest")
async def suggest_connector(req: ConnectorSuggestRequest, scope: Scope = Depends(resolve_scope)) -> dict:
    """AI-assisted connector discovery: given an API URL and a desired capability,
    probe the URL (MCP list_tools, else an OpenAPI/FastAPI spec), then have the LLM
    pick the relevant tool/endpoint and propose a connector config the operator can
    apply. Fetches an operator-supplied URL server-side — Studio (admin) surface."""
    import json as _json

    from ..bench.llm import client
    from ..config import get_settings

    if not req.instruction.strip():
        return {"error": "empty instruction"}
    urls = _extract_urls(req.instruction, *[str(h.get("content", "")) for h in reversed(req.history)])
    if not urls:
        return {"reply": "Give me the API base URL (e.g. https://host:9621) and what you want it to do — "
                         "I'll probe it for MCP tools or an OpenAPI spec and suggest a connector.", "suggestion": None}
    probed = urls[0]
    discovered = await _probe_mcp(probed) or await _probe_openapi(probed)
    if not discovered:
        return {"probed_url": probed, "discovered": {"kind": "none", "count": 0, "items": []},
                "reply": f"I couldn't discover an API surface at {probed}. It didn't respond as an MCP server "
                         f"(list_tools) and I found no OpenAPI spec at /openapi.json. Check the URL/port is reachable "
                         f"from the SOPilot backend, or paste the exact MCP or /openapi.json URL.", "suggestion": None}

    if discovered["kind"] == "mcp":
        surface = "DISCOVERED MCP TOOLS (base_url " + discovered["base_url"] + "):\n" + "\n".join(
            f"  - {it['name']}({', '.join(it['args'])}) — {it['description']}" for it in discovered["items"])
    else:
        surface = (f"DISCOVERED OPENAPI ENDPOINTS (title {discovered.get('title','')!r}, base_url "
                   + discovered["base_url"] + "):\n" + "\n".join(
                       f"  - {it['method']} {it['path']} — {it['summary']} {it['description']}".rstrip()
                       for it in discovered["items"]))
    user = surface + "\n\nWHAT THE OPERATOR WANTS:\n" + req.instruction[:1000]
    hist_msgs = [
        {"role": "assistant" if h.get("role") == "assistant" else "user", "content": str(h.get("content", ""))[:1500]}
        for h in req.history[-10:] if h.get("content")
    ]
    try:
        res = await client().chat.completions.create(
            model=get_settings().builder_model,
            messages=[{"role": "system", "content": _SUGGEST_SYS}, *hist_msgs, {"role": "user", "content": user}],
            temperature=0.1, max_tokens=900, response_format={"type": "json_object"},
        )
        data = _json.loads(res.choices[0].message.content or "{}")
    except Exception as e:  # LLM/key issue — still return what we discovered
        return {"probed_url": probed, "discovered": discovered,
                "reply": f"Discovered {discovered['count']} {discovered['kind']} item(s) at {discovered['base_url']}, "
                         f"but suggestion drafting is unavailable ({type(e).__name__}).", "suggestion": None}

    sug = data.get("suggestion") if isinstance(data, dict) else None
    suggestion = None
    if isinstance(sug, dict) and sug.get("kind") in CONNECTOR_KINDS and isinstance(sug.get("config"), dict):
        # never nest a connector ref; drop null/empty values the model left as placeholders
        cfg = {k: v for k, v in sug["config"].items() if k != "connector" and v not in (None, "")}
        suggestion = {"kind": sug["kind"], "name": str(sug.get("name") or "").strip(),
                      "description": str(sug.get("description") or "")[:200], "config": cfg}
    # "why is this useful" notes, keyed to the discovered items so the UI can annotate them
    why: dict[str, str] = {}
    for r in (data.get("relevant") if isinstance(data, dict) else None) or []:
        if isinstance(r, dict) and r.get("id"):
            why[str(r["id"]).strip()] = str(r.get("why") or "")[:160]
    return {"probed_url": probed,
            "discovered": {"kind": discovered["kind"], "base_url": discovered["base_url"],
                           "count": discovered["count"], "items": discovered["items"][:40]},
            "relevant": why,
            "reply": str((data or {}).get("reply") or ""), "suggestion": suggestion}


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

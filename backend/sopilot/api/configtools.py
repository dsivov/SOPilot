"""Config-management endpoints for the Studio Config viewer.

Live MCP introspection: given a config's mcp_servers, run list_tools against each
so the viewer can check the prompt's mcp_* references against the tools the
servers actually provide (the pain PolarTie engineers named).
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import (
    ConfigAnalysisReport, ConfigAnalysisReportVersion,
    ConfigDocument, ConfigDocumentVersion,
    ConfigRuleset, ConfigRulesetVersion,
)
from ..tenancy import Scope, resolve_scope

router = APIRouter(prefix="/config", tags=["config"])


class McpServerIn(BaseModel):
    url: str
    authorization: str | None = None


class IntrospectRequest(BaseModel):
    servers: list[McpServerIn] = []


async def _introspect_one(s: McpServerIn) -> dict:
    # fastmcp infers Streamable HTTP from an http(s) URL; auth is a bearer string.
    from fastmcp import Client
    try:
        async with Client(s.url, auth=s.authorization) as client:
            tools = await asyncio.wait_for(client.list_tools(), timeout=15)
        return {"url": s.url, "tools": [t.name for t in tools]}
    except Exception as e:  # unreachable / auth / protocol — surface, don't 500
        return {"url": s.url, "error": f"{type(e).__name__}: {str(e)[:200]}"}


@router.post("/introspect-mcp")
async def introspect_mcp(req: IntrospectRequest, scope: Scope = Depends(resolve_scope)) -> dict:
    """Run list_tools against each MCP server in the request, concurrently.

    Returns {"results": [{url, tools:[...]} | {url, error}]}. Note: this fetches
    arbitrary URLs server-side — allowlist / restrict to configured connectors
    before exposing to untrusted multi-tenant callers.
    """
    if not req.servers:
        return {"results": []}
    results = await asyncio.gather(*[_introspect_one(s) for s in req.servers[:20]])
    return {"results": list(results)}


# ---------- Ruleset persistence (stage 1 → stage 2 handoff) ----------
#
# The admin's authored ruleset, versioned SopVersion-style: every save is an
# immutable new version; the ruleset row tracks latest_version and
# published_version. The PUBLISHED version is what the user stage (Config view)
# enforces — that's what makes "admin bounds user" real. One ruleset per project
# ("default") for now.

_RULE_KINDS = ("requires", "conflicts", "enum")


def _validate_rules(rules: list) -> str | None:
    """Shape-check a ruleset (the formal engine's three kinds). Returns an error
    string or None. Content beyond shape (predicate atoms) is the admin's call."""
    if not isinstance(rules, list):
        return "rules must be a list"
    for i, r in enumerate(rules):
        if not isinstance(r, dict) or r.get("kind") not in _RULE_KINDS:
            return f"rule {i}: kind must be one of {_RULE_KINDS}"
        if r.get("level") not in ("error", "warn"):
            return f"rule {i}: level must be 'error' or 'warn'"
        if not str(r.get("id", "")).strip():
            return f"rule {i}: missing id"
        need = {"requires": ("when", "needs"), "conflicts": ("a", "b"), "enum": ("field", "options")}[r["kind"]]
        for k in need:
            if not r.get(k):
                return f"rule {i} ({r['kind']}): missing {k}"
        if r["kind"] == "enum" and not isinstance(r["options"], list):
            return f"rule {i}: options must be a list"
    return None


async def _get_ruleset(db: AsyncSession, scope: Scope) -> ConfigRuleset | None:
    return (await db.execute(select(ConfigRuleset).where(
        ConfigRuleset.tenant_id == scope.tenant_id,
        ConfigRuleset.project_id == scope.project_id,
        ConfigRuleset.name == "default"))).scalar_one_or_none()


async def _version_row(db: AsyncSession, ruleset_id: str, version: int) -> ConfigRulesetVersion | None:
    return (await db.execute(select(ConfigRulesetVersion).where(
        ConfigRulesetVersion.ruleset_id == ruleset_id,
        ConfigRulesetVersion.version == version))).scalar_one_or_none()


_FIELD_TYPES = ("string", "number", "boolean", "enum", "secret-ref", "connector-ref")


def _validate_schema(schema) -> str | None:
    """Shape-check a config schema (the DSL). Returns an error string or None.
    Minimal by design — the schema DESCRIBES options; content curation is the
    admin's. Only `fields` is enforced in Phase 1 (tools/structures optional)."""
    if schema is None:
        return None
    if not isinstance(schema, dict):
        return "schema must be an object"
    fields = schema.get("fields")
    if fields is None:
        return "schema.fields is required"
    if not isinstance(fields, list):
        return "schema.fields must be a list"
    seen = set()
    for i, f in enumerate(fields):
        if not isinstance(f, dict):
            return f"field {i}: must be an object"
        path = str(f.get("path", "")).strip()
        if not path:
            return f"field {i}: missing path"
        if path in seen:
            return f"field {i}: duplicate path '{path}'"
        seen.add(path)
        if f.get("type") not in _FIELD_TYPES:
            return f"field '{path}': type must be one of {_FIELD_TYPES}"
        if f.get("type") == "enum":
            opts = f.get("options")
            if opts is not None and not isinstance(opts, list):
                return f"field '{path}': enum options must be a list"
            # empty options allowed ONLY while the field awaits input (a Stage-0
            # analysis often knows a field is an enum before its allowed values).
            if not opts and f.get("status") != "needs_input":
                return f"field '{path}': enum requires options (unless status is 'needs_input')"
    return None


@router.get("/ruleset")
async def get_ruleset(scope: Scope = Depends(resolve_scope), db: AsyncSession = Depends(get_db)) -> dict:
    """The project's config profile: latest rules + schema (for the admin editor)
    and the published ones (what the user stage enforces / is bounded by).
    exists=False → nothing saved yet."""
    rs = await _get_ruleset(db, scope)
    if rs is None:
        return {"exists": False, "latest_version": 0, "published_version": None,
                "rules": None, "published_rules": None, "schema": None, "published_schema": None}
    latest = await _version_row(db, rs.id, rs.latest_version)
    published = await _version_row(db, rs.id, rs.published_version) if rs.published_version else None
    return {
        "exists": True,
        "latest_version": rs.latest_version,
        "published_version": rs.published_version,
        "rules": latest.rules if latest else None,
        "published_rules": published.rules if published else None,
        "schema": latest.config_schema if latest else None,
        "published_schema": published.config_schema if published else None,
    }


class RulesetSaveRequest(BaseModel):
    rules: list = []
    schema: dict | None = None   # the config DSL; null = leave unschematized (deriveFields fallback)
    publish: bool = False


@router.put("/ruleset")
async def save_ruleset(
    req: RulesetSaveRequest, scope: Scope = Depends(resolve_scope), db: AsyncSession = Depends(get_db)
) -> dict:
    """Save the config profile (rules + optional schema) as a NEW immutable
    version (SopVersion-style); optionally publish it. Publishing is what exposes
    it to the user stage."""
    err = _validate_rules(req.rules) or _validate_schema(req.schema)
    if err:
        raise HTTPException(status_code=422, detail=err)
    rs = await _get_ruleset(db, scope)
    if rs is None:
        rs = ConfigRuleset(tenant_id=scope.tenant_id, project_id=scope.project_id, name="default")
        db.add(rs)
        await db.flush()
    rs.latest_version += 1
    db.add(ConfigRulesetVersion(ruleset_id=rs.id, version=rs.latest_version,
                                rules=req.rules, config_schema=req.schema))
    if req.publish:
        rs.published_version = rs.latest_version
    await db.commit()
    return {"version": rs.latest_version, "published_version": rs.published_version}


@router.post("/ruleset/publish")
async def publish_ruleset(scope: Scope = Depends(resolve_scope), db: AsyncSession = Depends(get_db)) -> dict:
    """Publish the latest saved version — the moment the admin's bounds go live."""
    rs = await _get_ruleset(db, scope)
    if rs is None or rs.latest_version == 0:
        raise HTTPException(status_code=404, detail="no saved ruleset to publish")
    rs.published_version = rs.latest_version
    await db.commit()
    return {"version": rs.latest_version, "published_version": rs.published_version}


# ---------- Stage-0 analysis report: DB-versioned (Phase B) ----------
#
# Each analysis run is stored as a new immutable version (SopVersion-style),
# per project. The published version is the report the schema is synced from;
# re-analysis appends a version and the admin re-syncs (merge, client-side).

REPORT_KIND = "sopilot-system-analysis"


async def _get_report(db: AsyncSession, scope: Scope) -> ConfigAnalysisReport | None:
    return (await db.execute(select(ConfigAnalysisReport).where(
        ConfigAnalysisReport.tenant_id == scope.tenant_id,
        ConfigAnalysisReport.project_id == scope.project_id,
        ConfigAnalysisReport.name == "default"))).scalar_one_or_none()


async def _report_version(db: AsyncSession, report_id: str, version: int) -> dict | None:
    row = (await db.execute(select(ConfigAnalysisReportVersion).where(
        ConfigAnalysisReportVersion.report_id == report_id,
        ConfigAnalysisReportVersion.version == version))).scalar_one_or_none()
    return None if row is None else row.report


@router.get("/analysis")
async def get_analysis(scope: Scope = Depends(resolve_scope), db: AsyncSession = Depends(get_db)) -> dict:
    """The project's Stage-0 report: latest + published, plus the version list."""
    r = await _get_report(db, scope)
    if r is None:
        return {"exists": False, "latest_version": 0, "published_version": None,
                "report": None, "published_report": None, "versions": []}
    versions = (await db.execute(select(ConfigAnalysisReportVersion.version, ConfigAnalysisReportVersion.created_at)
        .where(ConfigAnalysisReportVersion.report_id == r.id)
        .order_by(ConfigAnalysisReportVersion.version.desc()))).all()
    return {
        "exists": True,
        "latest_version": r.latest_version,
        "published_version": r.published_version,
        "report": await _report_version(db, r.id, r.latest_version),
        "published_report": (await _report_version(db, r.id, r.published_version)) if r.published_version else None,
        "versions": [{"version": v, "created_at": c.isoformat()} for v, c in versions],
    }


class AnalysisSaveRequest(BaseModel):
    report: dict = {}
    publish: bool = True   # a new analysis run is normally the one to sync from


@router.put("/analysis")
async def save_analysis(
    req: AnalysisSaveRequest, scope: Scope = Depends(resolve_scope), db: AsyncSession = Depends(get_db)
) -> dict:
    """Store a Stage-0 report as a NEW immutable version; publish it by default
    (it becomes the report the schema syncs from)."""
    if req.report.get("kind") != REPORT_KIND:
        raise HTTPException(status_code=422, detail=f"not a {REPORT_KIND} report")
    if not isinstance(req.report.get("config_items"), list):
        raise HTTPException(status_code=422, detail="report.config_items must be a list")
    r = await _get_report(db, scope)
    if r is None:
        r = ConfigAnalysisReport(tenant_id=scope.tenant_id, project_id=scope.project_id, name="default")
        db.add(r)
        await db.flush()
    r.latest_version += 1
    db.add(ConfigAnalysisReportVersion(report_id=r.id, version=r.latest_version, report=req.report))
    if req.publish:
        r.published_version = r.latest_version
    await db.commit()
    return {"version": r.latest_version, "published_version": r.published_version}


# ---------- Config document: the robot config itself, DB-versioned ----------
#
# The config's durable home (replaces the browser working copy). Stage-2 saves
# create versions; the published version is the deploy config. One "default"
# per project.

async def _get_document(db: AsyncSession, scope: Scope) -> ConfigDocument | None:
    return (await db.execute(select(ConfigDocument).where(
        ConfigDocument.tenant_id == scope.tenant_id,
        ConfigDocument.project_id == scope.project_id,
        ConfigDocument.name == "default"))).scalar_one_or_none()


async def _document_version(db: AsyncSession, document_id: str, version: int) -> dict | None:
    row = (await db.execute(select(ConfigDocumentVersion).where(
        ConfigDocumentVersion.document_id == document_id,
        ConfigDocumentVersion.version == version))).scalar_one_or_none()
    return None if row is None else row.config


@router.get("/document")
async def get_document(scope: Scope = Depends(resolve_scope), db: AsyncSession = Depends(get_db)) -> dict:
    """The project's config document: latest (editing) + published (deploy),
    plus the version list. exists=False → nothing saved yet."""
    d = await _get_document(db, scope)
    if d is None:
        return {"exists": False, "latest_version": 0, "published_version": None,
                "config": None, "published_config": None, "versions": []}
    versions = (await db.execute(select(ConfigDocumentVersion.version, ConfigDocumentVersion.created_at)
        .where(ConfigDocumentVersion.document_id == d.id)
        .order_by(ConfigDocumentVersion.version.desc()))).all()
    return {
        "exists": True,
        "latest_version": d.latest_version,
        "published_version": d.published_version,
        "config": await _document_version(db, d.id, d.latest_version),
        "published_config": (await _document_version(db, d.id, d.published_version)) if d.published_version else None,
        "versions": [{"version": v, "created_at": c.isoformat()} for v, c in versions],
    }


class DocumentSaveRequest(BaseModel):
    config: dict = {}
    publish: bool = False


@router.put("/document")
async def save_document(
    req: DocumentSaveRequest, scope: Scope = Depends(resolve_scope), db: AsyncSession = Depends(get_db)
) -> dict:
    """Save the config as a NEW immutable version; optionally publish (deploy)."""
    if not isinstance(req.config, dict):
        raise HTTPException(status_code=422, detail="config must be an object")
    d = await _get_document(db, scope)
    if d is None:
        d = ConfigDocument(tenant_id=scope.tenant_id, project_id=scope.project_id, name="default")
        db.add(d)
        await db.flush()
    d.latest_version += 1
    db.add(ConfigDocumentVersion(document_id=d.id, version=d.latest_version, config=req.config))
    if req.publish:
        d.published_version = d.latest_version
    await db.commit()
    return {"version": d.latest_version, "published_version": d.published_version}


@router.post("/document/publish")
async def publish_document(scope: Scope = Depends(resolve_scope), db: AsyncSession = Depends(get_db)) -> dict:
    d = await _get_document(db, scope)
    if d is None or d.latest_version == 0:
        raise HTTPException(status_code=404, detail="no saved config to publish")
    d.published_version = d.latest_version
    await db.commit()
    return {"version": d.latest_version, "published_version": d.published_version}


# ---------- Write-back: render a deploy-ready robot config ----------
#
# The editor keeps connections as REFERENCES — an mcp_servers entry may carry
# {"connector": "<registry name>"} and/or authorization "secret:<name>". This
# endpoint resolves them server-side (connector registry URL + Fernet-decrypted
# tenant secret as a Bearer token) into the concrete config.json the robot
# deployment consumes. Secrets appear only in the rendered artifact the
# operator downloads — never in the editor state or the stored config.

class RenderRobotRequest(BaseModel):
    config: dict = {}


@router.post("/render-robot")
async def render_robot(
    req: RenderRobotRequest, scope: Scope = Depends(resolve_scope), db: AsyncSession = Depends(get_db)
) -> dict:
    import copy

    from ..models import Connector
    from ..secrets import get_secret

    cfg = copy.deepcopy(req.config)
    notes: list[str] = []
    resolved_servers: list[dict] = []
    for entry in cfg.get("mcp_servers") or []:
        if not isinstance(entry, dict):
            continue
        m = dict(entry)
        cname = m.pop("connector", None)
        if cname:
            conn = (await db.execute(select(Connector).where(
                Connector.tenant_id == scope.tenant_id, Connector.project_id == scope.project_id,
                Connector.name == cname))).scalar_one_or_none()
            if conn is None or conn.kind != "mcp":
                notes.append(f"connector '{cname}' not found (or not mcp) — entry kept as typed")
            else:
                if not conn.enabled:
                    notes.append(f"connector '{cname}' is disabled in the registry")
                m["url"] = (conn.config or {}).get("server") or m.get("url", "")
                if (conn.config or {}).get("auth_secret") and not m.get("authorization"):
                    m["authorization"] = f"secret:{conn.config['auth_secret']}"
        auth = m.get("authorization")
        if isinstance(auth, str) and auth.startswith("secret:"):
            secret_name = auth[len("secret:"):]
            value = await get_secret(db, scope.tenant_id, secret_name)
            if value is None:
                notes.append(f"secret '{secret_name}' not found — authorization left as the reference")
            else:
                m["authorization"] = value if value.startswith("Bearer ") else f"Bearer {value}"
        if not str(m.get("url", "")).strip():
            notes.append("dropped an mcp_servers entry without a url")
            continue
        resolved_servers.append(m)
    if "mcp_servers" in cfg or resolved_servers:
        cfg["mcp_servers"] = resolved_servers
    notes += await _schema_notes(db, scope, cfg)
    return {"config": cfg, "notes": notes}


async def _schema_notes(db: AsyncSession, scope: Scope, cfg: dict) -> list[str]:
    """Validate the config against the project's PUBLISHED schema (types, enum
    membership, required present). Advisory notes — write-back never blocks (the
    Studio already gates on rule errors); these catch schema drift at deploy."""
    rs = await _get_ruleset(db, scope)
    if rs is None or not rs.published_version:
        return []
    row = await _version_row(db, rs.id, rs.published_version)
    schema = row.config_schema if row else None
    if not schema or not isinstance(schema.get("fields"), list):
        return []

    def get_path(o, path):
        for k in path.split("."):
            o = o.get(k) if isinstance(o, dict) else None
            if o is None:
                return None
        return o

    out: list[str] = []
    for f in schema["fields"]:
        if not isinstance(f, dict):
            continue
        path, ftype = f.get("path", ""), f.get("type")
        v = get_path(cfg, path)
        if v is None or (isinstance(v, str) and not v.strip()):
            if f.get("required"):
                out.append(f"schema: required field '{path}' is not set")
            continue
        if ftype == "number" and not isinstance(v, (int, float)):
            out.append(f"schema: field '{path}' should be a number (got {type(v).__name__})")
        elif ftype == "boolean" and not isinstance(v, bool):
            out.append(f"schema: field '{path}' should be a boolean (got {type(v).__name__})")
        elif ftype == "enum" and f.get("options") and str(v) not in [str(o) for o in f["options"]]:
            out.append(f"schema: field '{path}' = '{v}' is not one of {f['options']}")
    return out



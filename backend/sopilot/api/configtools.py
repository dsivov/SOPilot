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


class ValidatePromptRequest(BaseModel):
    prompt: str = ""
    available_tools: list[str] = []   # built-in enabled tools + mcp_<name> (from introspection)
    transfer_topics: list[str] = []
    language: str = ""


_VALIDATE_SYS = (
    "You review a voice-agent system prompt against the agent's ACTUAL configured capabilities, and flag LOGICAL "
    "problems only — never style or wording. Look for: (a) the prompt promising or offering something the agent "
    "cannot do — a capability or tool it does not have; (b) the prompt referencing a tool, an MCP tool (written "
    "mcp_<name>), a knowledge source, or a transfer target that is not in the AVAILABLE list; (c) internal "
    "contradictions in the instructions; (d) a conflict with the configured language. Report only real problems a "
    "caller would actually hit — do not invent issues. If the prompt is consistent with the capabilities, return a "
    "single ok finding. Return JSON: {\"findings\":[{\"level\":\"error\"|\"warn\"|\"ok\",\"msg\":\"<one concrete "
    "sentence, name the tool/capability>\"}]}. error = a broken promise / missing capability; warn = a likely gap; "
    "ok = a confirmation."
)


@router.post("/validate-prompt")
async def validate_prompt(req: ValidatePromptRequest, scope: Scope = Depends(resolve_scope)) -> dict:
    """LLM logical prompt validation: check the freeform prompt against the agent's
    real capabilities (enabled tools + introspected MCP tools). The pain the prod
    team named — a prompt promising or referencing things the config can't deliver."""
    import json as _json

    from ..bench.llm import client
    from ..config import get_settings
    if not req.prompt.strip():
        return {"findings": []}
    user = (
        "AVAILABLE TOOLS (the only capabilities the agent has):\n"
        + (", ".join(req.available_tools) or "(none)")
        + "\n\nTRANSFER TARGETS: " + (", ".join(req.transfer_topics) or "(none)")
        + "\nCONFIGURED LANGUAGE: " + (req.language or "(unset)")
        + "\n\nSYSTEM PROMPT:\n" + req.prompt[:8000]
    )
    try:
        res = await client().chat.completions.create(
            model=get_settings().builder_model,
            messages=[{"role": "system", "content": _VALIDATE_SYS}, {"role": "user", "content": user}],
            temperature=0.2, max_tokens=700, response_format={"type": "json_object"},
        )
        data = _json.loads(res.choices[0].message.content or "{}")
    except Exception as e:  # LLM/key issue — degrade, don't 500
        return {"findings": [{"level": "warn", "msg": f"prompt validation unavailable ({type(e).__name__})"}]}
    out = []
    for f in (data.get("findings") if isinstance(data, dict) else None) or []:
        if not isinstance(f, dict):
            continue
        lvl = f.get("level")
        out.append({"level": lvl if lvl in ("error", "warn", "ok") else "warn", "msg": str(f.get("msg", ""))[:400]})
    return {"findings": out[:25] or [{"level": "ok", "msg": "No logical inconsistencies found."}]}


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


class DraftFieldRequest(BaseModel):
    instruction: str = ""              # admin's plain-English field description
    existing_fields: list[str] = []    # field paths already declared (avoid dupes)
    known_paths: list[str] = []        # dot-paths seen in real configs (grounding)


_DRAFT_FIELD_SYS = (
    "You are the admin-stage schema assistant of a voice-agent configuration manager. Turn ONE plain-English "
    "description of a configuration option into ONE structured schema field definition (the config DSL). Return "
    "ONLY JSON: {\"field\": {\"path\": <dot.path>, \"type\": \"string\"|\"number\"|\"boolean\"|\"enum\"|"
    "\"secret-ref\"|\"connector-ref\", \"options\": [<enum values, only when type is enum>], \"required\": "
    "true|false, \"description\": <one-sentence help shown to the user>, \"advanced\": true|false}} or, if the "
    "request is not a single declarable field, {\"error\": \"<why>\"}. Prefer a dot-path consistent with the "
    "KNOWN PATHS style; reuse an existing path only if the admin is redefining it. Use type enum with options "
    "only when the value is a fixed small set; secret-ref for credentials, connector-ref for a named connector. "
    "Keep description short and concrete."
)


@router.post("/draft-field")
async def draft_field(req: DraftFieldRequest, scope: Scope = Depends(resolve_scope)) -> dict:
    """LLM-assisted schema authoring (admin stage): plain English → one schema
    FieldDef. 'Chat to define the DSL' — the field the admin reviews and adds."""
    import json as _json

    from ..bench.llm import client
    from ..config import get_settings
    if not req.instruction.strip():
        return {"error": "empty instruction"}
    user = (
        "EXISTING FIELD PATHS (already declared):\n" + (", ".join(req.existing_fields[:80]) or "(none)")
        + "\n\nKNOWN PATHS seen in real configs (style reference):\n" + (", ".join(req.known_paths[:80]) or "(none)")
        + "\n\nDESCRIBE THE FIELD:\n" + req.instruction[:600]
    )
    try:
        res = await client().chat.completions.create(
            model=get_settings().builder_model,
            messages=[{"role": "system", "content": _DRAFT_FIELD_SYS}, {"role": "user", "content": user}],
            temperature=0.1, max_tokens=300, response_format={"type": "json_object"},
        )
        data = _json.loads(res.choices[0].message.content or "{}")
    except Exception as e:  # noqa: BLE001 — LLM/key issue, surface not 500
        return {"error": f"field drafting unavailable ({type(e).__name__})"}
    f = data.get("field") if isinstance(data, dict) else None
    if not isinstance(f, dict) or not str(f.get("path", "")).strip():
        return {"error": str(data.get("error") or "the model did not return a valid field")}
    ftype = f.get("type") if f.get("type") in _FIELD_TYPES else "string"
    out = {"path": str(f["path"]).strip(), "type": ftype,
           "description": str(f.get("description", ""))[:300],
           "required": bool(f.get("required")), "advanced": bool(f.get("advanced"))}
    if ftype == "enum":
        out["options"] = [str(o) for o in (f.get("options") or []) if isinstance(o, (str, int, float))]
        if not out["options"]:
            return {"error": "enum field needs options — rephrase with the allowed values"}
    return {"field": out}


class DraftEditRequest(BaseModel):
    instruction: str = ""              # the user's plain-English change request
    tools: list[dict] = []             # [{name, enabled}] — current tool states
    fields: list[dict] = []            # [{field, value, options?}] — editable scalars (+ enum options)
    rules: list[str] = []              # the admin ruleset, described (bounds shown to the LLM)
    prompt: str = ""                   # the agent's current global prompt
    structures: dict = {}              # current lists: mcp_servers (urls), knowledge_bases, transfer_topics (ids)


_DRAFT_EDIT_SYS = (
    "You are the user-stage assistant of a voice-agent configuration manager — you both ANSWER questions about the "
    "config and PROPOSE changes. Be helpful and conversational, never a terse rejector.\n\n"
    "When a request maps to a concrete change, propose the MINIMAL formal edit operations. The edit vocabulary is "
    "exactly:\n"
    "  {\"op\":\"enable_tool\",\"tool\":<name>}\n"
    "  {\"op\":\"disable_tool\",\"tool\":<name>}\n"
    "  {\"op\":\"set_field\",\"field\":<dot.path>,\"value\":<string>}\n"
    "  {\"op\":\"unset_field\",\"field\":<dot.path>}\n"
    "  {\"op\":\"set_prompt\",\"value\":<the full new agent prompt>}\n"
    "  {\"op\":\"append_prompt\",\"value\":<text to add to the agent prompt>}\n"
    "  {\"op\":\"add_mcp_server\",\"url\":<url>,\"authorization\":<optional bearer>}\n"
    "  {\"op\":\"remove_mcp_server\",\"url\":<existing url>}\n"
    "  {\"op\":\"add_kb\",\"knowledge_id\":<id>,\"index_mode\":\"simple\"|\"lightrag\",\"function_tag\":<optional>}\n"
    "  {\"op\":\"remove_kb\",\"knowledge_id\":<existing id>}\n"
    "  {\"op\":\"add_transfer_topic\",\"topic_id\":<id>,\"function_tag\":<optional>,\"prompt\":<when to transfer here>}\n"
    "  {\"op\":\"remove_transfer_topic\",\"topic_id\":<existing id>}\n"
    "Only reference tools/fields from the provided lists, and remove only listed structure entries — never invent "
    "names. Stay within the ADMIN RULES (allowed enum options only; if enabling a tool or adding a structure "
    "requires a field per a rule, include the set_field — placeholder value only when none can be inferred).\n\n"
    "HOW THIS CONFIG WORKS (use to answer 'how do I…' questions):\n"
    "- The agent PROMPT is the robot's global instructions/persona. To change or extend it, use set_prompt "
    "(replace the whole prompt) or append_prompt (add to it) — write the actual prompt text. It is the agent's "
    "behavior when no SOP is bound.\n"
    "- Built-in TOOLS are capabilities toggled on/off (send_email, transfer, show_table, …).\n"
    "- EXTERNAL DATA/KNOWLEDGE (weather, flight status, prices, any live or document data the agent should draw on) "
    "comes through a CONNECTOR — an MCP server, a RAG/HTTP endpoint, or a managed corpus — registered in the "
    "Connectors view. To add such data you: (1) register/point at the source, e.g. add an mcp_servers entry for a "
    "weather MCP; (2) optionally add a knowledge_base entry; (3) the prompt/SOP then references it. You can add the "
    "mcp_servers entry here if the user gives a URL; registering a named connector or editing the prompt happens in "
    "their own views.\n"
    "- TRANSFER TOPICS are handoff targets; KNOWLEDGE BASES are retrieval sources; scalar FIELDS are settings.\n\n"
    "Return ONLY JSON: {\"reply\": \"<a short, helpful answer to the user — always present; if you couldn't make an "
    "edit, explain concretely HOW to achieve what they asked and what you'd need from them, e.g. a URL>\", "
    "\"edits\": [<0 or more ops>]}. Always include a reply. Include edits only when you are proposing a concrete, "
    "in-vocabulary change; otherwise return an empty edits list and guide them in the reply."
)


@router.post("/draft-edit")
async def draft_edit(req: DraftEditRequest, scope: Scope = Depends(resolve_scope)) -> dict:
    """LLM-assisted guided editing (user stage): plain English → formal edit ops.
    The LLM only PROPOSES — the client re-evaluates the admin ruleset on the
    edited draft and blocks proposals that violate it. Two-stage thesis intact:
    LLM assists, the formal engine decides."""
    import json as _json

    from ..bench.llm import client
    from ..config import get_settings
    if not req.instruction.strip():
        return {"error": "empty instruction"}
    user = (
        "TOOLS (name · enabled):\n"
        + "\n".join(f"  {t.get('name')} · {'on' if t.get('enabled') else 'off'}" for t in req.tools[:80])
        + "\n\nEDITABLE FIELDS (field · current value · allowed options if enum-bound):\n"
        + "\n".join(
            f"  {f.get('field')} · {_json.dumps(f.get('value'))[:80]}"
            + (f" · one of {f['options']}" if f.get("options") else "")
            for f in req.fields[:40])
        + "\n\nADMIN RULES (the config must satisfy these):\n"
        + "\n".join(f"  - {r}" for r in req.rules[:40])
        + "\n\nCURRENT STRUCTURES:\n"
        + "  mcp_servers: " + _json.dumps((req.structures.get("mcp_servers") or [])[:20])
        + "\n  knowledge_bases: " + _json.dumps((req.structures.get("knowledge_bases") or [])[:20])
        + "\n  transfer_topics: " + _json.dumps((req.structures.get("transfer_topics") or [])[:20])
        + "\n\nCURRENT AGENT PROMPT:\n" + (req.prompt[:2000] or "(empty)")
        + "\n\nCHANGE REQUEST:\n" + req.instruction[:1000]
    )
    try:
        res = await client().chat.completions.create(
            model=get_settings().builder_model,
            messages=[{"role": "system", "content": _DRAFT_EDIT_SYS}, {"role": "user", "content": user}],
            # prompt edits can be long — allow room for a full rewritten prompt
            temperature=0.1, max_tokens=1500, response_format={"type": "json_object"},
        )
        data = _json.loads(res.choices[0].message.content or "{}")
    except Exception as e:  # LLM/key issue — surface, don't 500
        return {"error": f"edit drafting unavailable ({type(e).__name__})"}
    edits_in = data.get("edits") if isinstance(data, dict) else None
    edits: list[dict] = []
    for e in edits_in or []:
        if not isinstance(e, dict):
            continue
        op = e.get("op")
        if op in ("enable_tool", "disable_tool") and e.get("tool"):
            edits.append({"op": op, "tool": str(e["tool"])})
        elif op == "set_field" and e.get("field"):
            edits.append({"op": op, "field": str(e["field"]), "value": str(e.get("value", ""))})
        elif op == "unset_field" and e.get("field"):
            edits.append({"op": op, "field": str(e["field"])})
        elif op in ("set_prompt", "append_prompt") and e.get("value"):
            edits.append({"op": op, "value": str(e["value"])[:8000]})
        elif op in ("add_mcp_server", "remove_mcp_server") and e.get("url"):
            out_e = {"op": op, "url": str(e["url"])}
            if op == "add_mcp_server" and e.get("authorization"):
                out_e["authorization"] = str(e["authorization"])
            edits.append(out_e)
        elif op in ("add_kb", "remove_kb") and e.get("knowledge_id"):
            out_e = {"op": op, "knowledge_id": str(e["knowledge_id"])}
            if op == "add_kb":
                out_e["index_mode"] = e.get("index_mode") if e.get("index_mode") in ("simple", "lightrag") else "simple"
                if e.get("function_tag"):
                    out_e["function_tag"] = str(e["function_tag"])
            edits.append(out_e)
        elif op in ("add_transfer_topic", "remove_transfer_topic") and e.get("topic_id"):
            out_e = {"op": op, "topic_id": str(e["topic_id"])}
            if op == "add_transfer_topic":
                for k in ("function_tag", "prompt"):
                    if e.get(k):
                        out_e[k] = str(e[k])
            edits.append(out_e)
    reply = str(data.get("reply") or data.get("note") or "").strip()[:1200]
    return {"edits": edits[:20], "reply": reply, "note": reply}  # note kept for back-compat


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


class DraftRuleRequest(BaseModel):
    instruction: str = ""              # the admin's plain-English constraint
    tools: list[str] = []              # config tool names, for grounding the predicates
    fields: list[str] = []             # config field paths (dot notation)


_DRAFT_SYS = (
    "You are the admin-stage assistant of a configuration manager. Convert ONE plain-English configuration "
    "constraint into ONE structured rule the formal engine can evaluate. The engine supports exactly three rule "
    "kinds and one predicate vocabulary — do not invent others.\n"
    "PREDICATE (a string atom over the config):\n"
    "  tool:<name>        a built-in tool is enabled; use | for any-of, e.g. tool:send_email|send_sms\n"
    "  field:<dot.path>   a config field is set / non-empty, e.g. field:notification_service_url\n"
    "  kb_mode:<mode>     some knowledge_base entry uses this index_mode, e.g. kb_mode:lightrag\n"
    "RULE KINDS (return the matching shape):\n"
    "  requires : {\"kind\":\"requires\",\"when\":<pred>,\"needs\":<pred>}   (if when holds, needs must hold)\n"
    "  conflicts: {\"kind\":\"conflicts\",\"a\":<pred>,\"b\":<pred>}          (a and b must not both hold)\n"
    "  enum     : {\"kind\":\"enum\",\"field\":<dot.path>,\"options\":[..]}   (field must be one of options)\n"
    "Only reference tools/fields from the provided vocabulary; if the instruction needs an atom that isn't there, "
    "pick the closest valid one. Also return \"level\" (\"error\" for a hard break, \"warn\" for a likely issue), a "
    "short kebab-case \"id\", and a one-sentence \"msg\" a user would see when the rule is violated (name the "
    "tool/field). Return ONLY the JSON object for the rule, nothing else."
)


@router.post("/draft-rule")
async def draft_rule(req: DraftRuleRequest, scope: Scope = Depends(resolve_scope)) -> dict:
    """LLM-assisted rule authoring (admin stage): turn a plain-English constraint into
    one structured enum/requires/conflicts rule the formal engine evaluates. 'The admin
    authors the rules with LLM help' — the rules stay content, the engine stays formal."""
    import json as _json

    from ..bench.llm import client
    from ..config import get_settings
    if not req.instruction.strip():
        return {"error": "empty instruction"}
    user = (
        "AVAILABLE TOOLS: " + (", ".join(req.tools) or "(none)")
        + "\nAVAILABLE FIELDS: " + (", ".join(req.fields) or "(none)")
        + "\n\nCONSTRAINT TO ENCODE:\n" + req.instruction[:1000]
    )
    try:
        res = await client().chat.completions.create(
            model=get_settings().builder_model,
            messages=[{"role": "system", "content": _DRAFT_SYS}, {"role": "user", "content": user}],
            temperature=0.1, max_tokens=400, response_format={"type": "json_object"},
        )
        data = _json.loads(res.choices[0].message.content or "{}")
    except Exception as e:  # LLM/key issue — surface, don't 500
        return {"error": f"rule drafting unavailable ({type(e).__name__})"}
    if not isinstance(data, dict) or data.get("kind") not in ("requires", "conflicts", "enum"):
        return {"error": "the model did not return a valid rule"}
    rule: dict = {
        "id": str(data.get("id") or "drafted-rule")[:60],
        "kind": data["kind"],
        "level": data.get("level") if data.get("level") in ("error", "warn") else "error",
        "msg": str(data.get("msg", ""))[:400],
    }
    if data["kind"] == "requires":
        rule["when"] = str(data.get("when", "")); rule["needs"] = str(data.get("needs", ""))
    elif data["kind"] == "conflicts":
        rule["a"] = str(data.get("a", "")); rule["b"] = str(data.get("b", ""))
    else:
        rule["field"] = str(data.get("field", ""))
        rule["options"] = [str(o) for o in (data.get("options") or []) if isinstance(o, (str, int, float))]
    return {"rule": rule}

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
from ..models import ConfigRuleset, ConfigRulesetVersion
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


async def _version_rules(db: AsyncSession, ruleset_id: str, version: int) -> list | None:
    row = (await db.execute(select(ConfigRulesetVersion).where(
        ConfigRulesetVersion.ruleset_id == ruleset_id,
        ConfigRulesetVersion.version == version))).scalar_one_or_none()
    return None if row is None else row.rules


@router.get("/ruleset")
async def get_ruleset(scope: Scope = Depends(resolve_scope), db: AsyncSession = Depends(get_db)) -> dict:
    """The project's ruleset: latest rules (for the admin editor) and published
    rules (what the user stage enforces). exists=False → nothing saved yet."""
    rs = await _get_ruleset(db, scope)
    if rs is None:
        return {"exists": False, "latest_version": 0, "published_version": None, "rules": None, "published_rules": None}
    return {
        "exists": True,
        "latest_version": rs.latest_version,
        "published_version": rs.published_version,
        "rules": await _version_rules(db, rs.id, rs.latest_version),
        "published_rules": (await _version_rules(db, rs.id, rs.published_version)) if rs.published_version else None,
    }


class RulesetSaveRequest(BaseModel):
    rules: list = []
    publish: bool = False


@router.put("/ruleset")
async def save_ruleset(
    req: RulesetSaveRequest, scope: Scope = Depends(resolve_scope), db: AsyncSession = Depends(get_db)
) -> dict:
    """Save the ruleset as a NEW immutable version (SopVersion-style); optionally
    publish it in the same call. Publishing is what exposes it to the user stage."""
    err = _validate_rules(req.rules)
    if err:
        raise HTTPException(status_code=422, detail=err)
    rs = await _get_ruleset(db, scope)
    if rs is None:
        rs = ConfigRuleset(tenant_id=scope.tenant_id, project_id=scope.project_id, name="default")
        db.add(rs)
        await db.flush()
    rs.latest_version += 1
    db.add(ConfigRulesetVersion(ruleset_id=rs.id, version=rs.latest_version, rules=req.rules))
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


class DraftEditRequest(BaseModel):
    instruction: str = ""              # the user's plain-English change request
    tools: list[dict] = []             # [{name, enabled}] — current tool states
    fields: list[dict] = []            # [{field, value, options?}] — editable scalars (+ enum options)
    rules: list[str] = []              # the admin ruleset, described (bounds shown to the LLM)


_DRAFT_EDIT_SYS = (
    "You are the user-stage assistant of a configuration manager. Convert ONE plain-English change request into "
    "formal edit operations the guided editor can apply. The edit vocabulary is exactly:\n"
    "  {\"op\":\"enable_tool\",\"tool\":<name>}\n"
    "  {\"op\":\"disable_tool\",\"tool\":<name>}\n"
    "  {\"op\":\"set_field\",\"field\":<dot.path>,\"value\":<string>}\n"
    "  {\"op\":\"unset_field\",\"field\":<dot.path>}\n"
    "Only reference tools and fields from the provided lists — never invent names. The ADMIN RULES bound what a "
    "valid config may look like: your proposal must keep the config within them (use only allowed enum options; "
    "if enabling a tool requires a field per a rule, include a set_field for it — ask for a placeholder value "
    "only when none can be inferred). Propose the MINIMAL set of edits for the request. Return ONLY JSON: "
    "{\"edits\":[...], \"note\":\"<one sentence: what the edits do and any caveat>\"}. If the request cannot be "
    "done within the vocabulary or would necessarily violate a rule, return {\"edits\":[], \"note\":\"<why>\"}."
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
        + "\n\nCHANGE REQUEST:\n" + req.instruction[:1000]
    )
    try:
        res = await client().chat.completions.create(
            model=get_settings().builder_model,
            messages=[{"role": "system", "content": _DRAFT_EDIT_SYS}, {"role": "user", "content": user}],
            temperature=0.1, max_tokens=500, response_format={"type": "json_object"},
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
    return {"edits": edits[:20], "note": str(data.get("note", ""))[:400]}


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

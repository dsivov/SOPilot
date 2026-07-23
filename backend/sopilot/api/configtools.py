"""Config-management endpoints for the Studio Config viewer.

Live MCP introspection: given a config's mcp_servers, run list_tools against each
so the viewer can check the prompt's mcp_* references against the tools the
servers actually provide (the pain PolarTie engineers named).
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends
from pydantic import BaseModel

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

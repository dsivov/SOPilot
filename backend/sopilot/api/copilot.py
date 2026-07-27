"""The unified SOPilot copilot — one assistant across every Studio tab.

Unlike the per-tab one-shot helpers it replaces, this endpoint is:
  - persistent   — the conversation lives in the DB (copilot_threads/messages),
                   so history survives reloads and tab switches (P5).
  - state-aware  — it assembles the project's PUBLISHED artifacts (schema, rules,
                   config, SOPs, prompt blocks, connectors, analysis report) so it
                   knows what's already defined and configured.
  - remembering  — it accumulates durable, per-TENANT memory (copilot_memories):
                   discovered APIs, decisions, established facts — never lost, and
                   deletable when stale context causes problems.
  - role-aware   — it reports admin-defined limits DOWN to users and Stage-0
                   discovered gaps UP to admins.
"""
from __future__ import annotations

import json as _json

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import (
    ApiKey, Connector, ConfigAnalysisReport, ConfigAnalysisReportVersion,
    ConfigDocument, ConfigDocumentVersion, ConfigRuleset, ConfigRulesetVersion,
    CopilotMemory, CopilotMessage, CopilotThread, PromptBlock, Sop,
)
from ..tenancy import Scope, hash_api_key, resolve_scope

router = APIRouter(prefix="/copilot", tags=["copilot"])

_HISTORY_TURNS = 24   # how many prior turns to replay into the model
_MEMORY_MAX = 60      # memories injected into context


# ---------- role (RBAC-aware guidance) ----------

async def resolve_role(authorization: str = Header(default=""), db: AsyncSession = Depends(get_db)) -> str:
    """The current key's role: 'admin' or 'user' (runtime keys → user)."""
    if not authorization.startswith("Bearer "):
        return "user"
    kh = hash_api_key(authorization.removeprefix("Bearer ").strip())
    role = (await db.execute(select(ApiKey.role).where(ApiKey.key_hash == kh, ApiKey.revoked_at.is_(None)))).scalar_one_or_none()
    return "admin" if role == "admin" else "user"


# ---------- requests ----------

class AssistRequest(BaseModel):
    tab: str = ""                      # the view the user is on (config, sops, connectors, …)
    instruction: str = ""              # the user's message
    snapshot: dict = {}                # the view's current working object (unsaved edits, selection)


class MemoryUpsert(BaseModel):
    title: str
    content: str
    kind: str = "fact"                 # fact | discovery | decision | preference
    source: str = ""
    project_scoped: bool = False       # default tenant-wide; True pins it to this project


# ---------- published-state assembler (what's already defined & configured) ----------

async def _published_version_payload(db, parent_model, version_model, fk_field, scope, cols):
    """Fetch the published version row's columns for a per-project versioned artifact."""
    parent = (await db.execute(select(parent_model).where(
        parent_model.tenant_id == scope.tenant_id, parent_model.project_id == scope.project_id,
        parent_model.name == "default"))).scalar_one_or_none()
    if parent is None or parent.published_version is None:
        return None
    row = (await db.execute(select(*cols).where(
        getattr(version_model, fk_field) == parent.id, version_model.version == parent.published_version))).first()
    return row


async def _assemble_state(db: AsyncSession, scope: Scope) -> dict:
    """A compact, structured summary of the project's PUBLISHED artifacts — the
    'what already exists' the copilot reasons over."""
    state: dict = {}

    rs = await _published_version_payload(db, ConfigRuleset, ConfigRulesetVersion, "ruleset_id", scope,
                                          [ConfigRulesetVersion.rules, ConfigRulesetVersion.config_schema])
    if rs:
        rules, schema = rs[0] or [], rs[1] or {}
        state["config_schema"] = {
            "fields": [{"path": f.get("path"), "type": f.get("type"), "options": f.get("options"),
                        "required": f.get("required"), "status": f.get("status")} for f in (schema.get("fields") or [])],
            "tools": [t.get("name") for t in (schema.get("tools") or [])],
            "structures": [s.get("key") for s in (schema.get("structures") or [])],
        }
        state["rules"] = rules

    doc = await _published_version_payload(db, ConfigDocument, ConfigDocumentVersion, "document_id", scope,
                                           [ConfigDocumentVersion.config])
    if doc and doc[0]:
        c = doc[0]
        state["config"] = {
            "display_name": c.get("display_name"), "voice": c.get("voice"),
            "default_language_iso": c.get("default_language_iso"),
            "enabled_tools": [k for k, v in (c.get("tools") or {}).items() if isinstance(v, dict) and v.get("enabled")],
            "mcp_servers": [m.get("url") or m.get("connector") for m in (c.get("mcp_servers") or [])],
            "knowledge_base": [k.get("knowledge_id") for k in (c.get("knowledge_base") or [])],
            "transfer_topics": [t.get("topic_id") for t in (c.get("transfer_topics") or [])],
            "has_prompt": bool(c.get("prompt")),
        }

    rep = await _published_version_payload(db, ConfigAnalysisReport, ConfigAnalysisReportVersion, "report_id", scope,
                                           [ConfigAnalysisReportVersion.report])
    if rep and rep[0]:
        r = rep[0]
        # STAGE-0 DISCOVERED SURFACE — the functionality map that BOUNDS what an
        # admin may declare in Stage 1. Anything not here isn't in the system.
        state["stage0_discovered"] = {
            "fields": [c.get("path") for c in (r.get("config_items") or []) if c.get("item") == "field"],
            "tools": [c.get("path") for c in (r.get("config_items") or []) if c.get("item") == "tool"],
            "structures": [c.get("path") for c in (r.get("config_items") or []) if c.get("item") == "structure"],
            "integration_points": [{"name": p.get("name"), "kind": p.get("kind")} for p in (r.get("integration_points") or [])],
            "components": [{"name": c.get("name"), "kind": c.get("kind")} for c in (r.get("components") or [])],
        }
        state["analysis_open_questions"] = [
            {"about": q.get("about"), "question": q.get("question"), "needs_from": q.get("needs_from")}
            for q in (r.get("open_questions") or [])]
        state["analysis_needs_input"] = [
            c.get("path") for c in (r.get("config_items") or []) if c.get("status") == "needs_input"]

    conns = (await db.execute(select(Connector.name, Connector.kind, Connector.enabled).where(
        Connector.tenant_id == scope.tenant_id, Connector.project_id == scope.project_id))).all()
    if conns:
        state["connectors"] = [{"name": n, "kind": k, "enabled": e} for n, k, e in conns]

    sops = (await db.execute(select(Sop.name).where(
        Sop.tenant_id == scope.tenant_id, Sop.project_id == scope.project_id).limit(40))).scalars().all()
    if sops:
        state["sops"] = list(sops)

    blocks = (await db.execute(select(PromptBlock.name).where(
        PromptBlock.tenant_id == scope.tenant_id, PromptBlock.project_id == scope.project_id).limit(40))).scalars().all()
    if blocks:
        state["prompt_blocks"] = list(blocks)

    return state


async def _get_thread(db: AsyncSession, scope: Scope, create: bool = False) -> CopilotThread | None:
    t = (await db.execute(select(CopilotThread).where(
        CopilotThread.tenant_id == scope.tenant_id, CopilotThread.project_id == scope.project_id))).scalar_one_or_none()
    if t is None and create:
        t = CopilotThread(tenant_id=scope.tenant_id, project_id=scope.project_id)
        db.add(t)
        await db.flush()
    return t


async def _load_memory(db: AsyncSession, scope: Scope) -> list[CopilotMemory]:
    """Tenant-wide memory + this project's memory, newest first."""
    rows = (await db.execute(select(CopilotMemory).where(
        CopilotMemory.tenant_id == scope.tenant_id,
        (CopilotMemory.project_id.is_(None)) | (CopilotMemory.project_id == scope.project_id))
        .order_by(CopilotMemory.updated_at.desc()).limit(_MEMORY_MAX))).scalars().all()
    return list(rows)


# ---------- the brain ----------

_SYS = (
    "You are the SOPilot copilot — ONE assistant that helps across every tab of the SOPilot Studio, a platform for "
    "building and governing voice-agent behavior. You guide the user step by step: explain what a feature does, offer "
    "concrete options, and warn about problems — you are a knowledgeable co-pilot, never a terse tool.\n\n"
    "THE PLATFORM (tabs you help with):\n"
    "- SOPs: versioned procedures the agent follows (actions, user-states, data dependencies, prompt blocks).\n"
    "- Prompt blocks: reusable instruction snippets.\n"
    "- Connectors: data sources (MCP / RAG / HTTP) defined once and reused by SOPs and configs.\n"
    "- Config admin (STAGE 1): the admin declares the config SCHEMA (available options, typed) and RULES "
    "(enum/requires/conflicts) — the bounds users must stay within.\n"
    "- Config viewer (STAGE 2): the user authors the robot config within those bounds; edits are gated by the rules.\n"
    "- Analysis report (STAGE 0): system discovery; open_questions and needs_input flag what's unknown.\n\n"
    "YOU ARE STATE-AWARE. The user message includes CURRENT STATE (what's already defined & configured) and MEMORY "
    "(durable facts gathered earlier). Use them: don't re-ask what's known; reference existing connectors/tools/rules by "
    "name; build on prior decisions.\n\n"
    "THE BOUNDING CHAIN — enforce it strictly. Each role/stage is limited by the previous one:\n"
    "- STAGE 0 (discovery) defines the functionality that actually EXISTS — see state.stage0_discovered "
    "(fields/tools/structures/integration_points/components).\n"
    "- STAGE 1 (ADMIN) is bounded BY Stage 0: an admin may only declare schema fields/tools/structures that Stage-0 "
    "discovered. If an admin asks to add functionality NOT in stage0_discovered, do NOT invent it — warn that it isn't in "
    "the discovered system surface and tell them to raise it as an open question to Engineering (Stage 0) so the system is "
    "analysed/extended first. If no Stage-0 report exists yet, say discovery must run first.\n"
    "- STAGE 2 (USER) is bounded BY Stage 1: a user may only choose within the admin's published SCHEMA (allowed fields, "
    "enum options, offerable tools, available structures) and must satisfy the RULES. If a user asks for something outside "
    "those bounds, do NOT propose it — explain it isn't permitted by the admin's configuration and that they should "
    "request the admin add/allow it.\n\n"
    "ROLE CAPABILITIES — stay in lane:\n"
    "- ADMIN: works in Stage 0/1 (analysis, schema, rules) and may inspect Stage 2. Bounded by Stage 0.\n"
    "- USER: works in Stage 2 only (authoring the config within bounds). If a USER asks to change the schema or rules "
    "(an admin action), decline and direct them to their admin — do not help a user widen their own bounds.\n\n"
    "RAISE WARNINGS for logical problems or wrong usage: a prompt promising a capability the config lacks; enabling a "
    "tool whose required field is unset; referencing a connector/tool/SOP that doesn't exist; a config value outside the "
    "admin's allowed options; an admin declaring an option with no Stage-0 backing.\n\n"
    "REPORT LIMITATIONS by role:\n"
    "- To a USER: surface the admin-defined bounds relevant to their request (e.g. 'voice is restricted to X/Y'; "
    "'send_email requires a notification service').\n"
    "- To an ADMIN: surface Stage-0 discovered gaps that need them — analysis_open_questions with needs_from=admin, and "
    "analysis_needs_input fields still awaiting definition; and flag anything they ask for that Stage 0 didn't surface.\n\n"
    "REMEMBER durable facts worth keeping for later (a discovered API and its useful endpoints, a decision the user made, "
    "a stated preference) in the 'remember' array — these persist per tenant and must not be trivial or transient.\n\n"
    "Return ONLY JSON: {\"reply\": \"<your helpful, specific answer — always present>\", "
    "\"warnings\": [{\"level\": \"warn\"|\"error\", \"msg\": \"<one concrete sentence>\"}], "
    "\"remember\": [{\"title\": \"<short>\", \"content\": \"<the fact>\", \"kind\": \"fact|discovery|decision|preference\"}]}. "
    "warnings and remember may be empty arrays. Keep the reply concise and actionable."
)


def _memory_block(mems: list[CopilotMemory]) -> str:
    if not mems:
        return "(none yet)"
    return "\n".join(f"  - [{m.kind}] {m.title}: {m.content}"[:400] for m in mems)


@router.post("/assist")
async def assist(req: AssistRequest, scope: Scope = Depends(resolve_scope),
                 role: str = Depends(resolve_role), db: AsyncSession = Depends(get_db)) -> dict:
    from ..bench.llm import client
    from ..config import get_settings

    if not req.instruction.strip():
        return {"error": "empty instruction"}

    thread = await _get_thread(db, scope, create=True)
    prior = (await db.execute(select(CopilotMessage).where(CopilotMessage.thread_id == thread.id)
             .order_by(CopilotMessage.created_at.desc()).limit(_HISTORY_TURNS))).scalars().all()
    prior = list(reversed(prior))
    hist_msgs = [{"role": m.role if m.role in ("user", "assistant") else "user", "content": m.content[:2000]} for m in prior]

    state = await _assemble_state(db, scope)
    mems = await _load_memory(db, scope)
    user = (
        f"ROLE: {role}\nCURRENT TAB: {req.tab or 'unknown'}\n\n"
        f"CURRENT STATE (published / what already exists):\n{_json.dumps(state, indent=1)[:6000]}\n\n"
        f"CURRENT VIEW SNAPSHOT (unsaved working object on this tab):\n{_json.dumps(req.snapshot)[:3000]}\n\n"
        f"MEMORY (durable, gathered earlier):\n{_memory_block(mems)}\n\n"
        f"USER MESSAGE:\n{req.instruction[:2000]}"
    )
    try:
        res = await client().chat.completions.create(
            model=get_settings().builder_model,
            messages=[{"role": "system", "content": _SYS}, *hist_msgs, {"role": "user", "content": user}],
            temperature=0.2, max_tokens=1200, response_format={"type": "json_object"})
        data = _json.loads(res.choices[0].message.content or "{}")
    except Exception as e:  # LLM/key issue — persist the user turn, surface the failure
        db.add(CopilotMessage(thread_id=thread.id, role="user", tab=req.tab, content=req.instruction[:4000]))
        await db.commit()
        return {"error": f"copilot unavailable ({type(e).__name__})"}

    reply = str((data or {}).get("reply") or "")
    warnings = [{"level": "error" if w.get("level") == "error" else "warn", "msg": str(w.get("msg", ""))[:300]}
                for w in ((data.get("warnings") if isinstance(data, dict) else None) or []) if isinstance(w, dict) and w.get("msg")]

    # Persist durable memory the assistant chose to keep (dedupe by title, tenant-wide).
    remembered: list[str] = []
    for r in ((data.get("remember") if isinstance(data, dict) else None) or [])[:6]:
        if not isinstance(r, dict) or not r.get("title") or not r.get("content"):
            continue
        title = str(r["title"])[:200]
        existing = (await db.execute(select(CopilotMemory).where(
            CopilotMemory.tenant_id == scope.tenant_id, CopilotMemory.title == title))).scalar_one_or_none()
        if existing:
            existing.content = str(r["content"])[:4000]
            existing.source = req.tab
        else:
            db.add(CopilotMemory(tenant_id=scope.tenant_id, project_id=None, kind=str(r.get("kind") or "fact")[:40],
                                 title=title, content=str(r["content"])[:4000], source=req.tab[:80]))
        remembered.append(title)

    # Persist the conversation turns.
    db.add(CopilotMessage(thread_id=thread.id, role="user", tab=req.tab, content=req.instruction[:4000]))
    db.add(CopilotMessage(thread_id=thread.id, role="assistant", tab=req.tab, content=reply[:8000],
                          meta={"warnings": warnings, "remembered": remembered} if (warnings or remembered) else None))
    await db.commit()

    return {"reply": reply, "warnings": warnings, "remembered": remembered, "role": role}


# ---------- conversation ----------

@router.get("/thread")
async def get_thread(scope: Scope = Depends(resolve_scope), db: AsyncSession = Depends(get_db)) -> dict:
    thread = await _get_thread(db, scope)
    if thread is None:
        return {"messages": []}
    rows = (await db.execute(select(CopilotMessage).where(CopilotMessage.thread_id == thread.id)
            .order_by(CopilotMessage.created_at).limit(200))).scalars().all()
    return {"messages": [{"role": m.role, "tab": m.tab, "content": m.content, "meta": m.meta,
                          "created_at": m.created_at.isoformat()} for m in rows]}


@router.delete("/thread")
async def clear_thread(scope: Scope = Depends(resolve_scope), db: AsyncSession = Depends(get_db)) -> dict:
    """Wipe the conversation (keeps durable memory). Use when the chat context
    gets muddled but the gathered facts are still good."""
    thread = await _get_thread(db, scope)
    if thread is None:
        return {"cleared": 0}
    await db.execute(sa_delete(CopilotMessage).where(CopilotMessage.thread_id == thread.id))
    await db.commit()
    return {"cleared": 1}


# ---------- durable memory ----------

@router.get("/memory")
async def list_memory(scope: Scope = Depends(resolve_scope), db: AsyncSession = Depends(get_db)) -> dict:
    mems = await _load_memory(db, scope)
    return {"memories": [{"id": m.id, "kind": m.kind, "title": m.title, "content": m.content,
                          "source": m.source, "tenant_wide": m.project_id is None,
                          "updated_at": m.updated_at.isoformat()} for m in mems]}


@router.post("/memory")
async def add_memory(req: MemoryUpsert, scope: Scope = Depends(resolve_scope), db: AsyncSession = Depends(get_db)) -> dict:
    if not req.title.strip() or not req.content.strip():
        raise HTTPException(status_code=422, detail="title and content required")
    existing = (await db.execute(select(CopilotMemory).where(
        CopilotMemory.tenant_id == scope.tenant_id, CopilotMemory.title == req.title.strip()))).scalar_one_or_none()
    if existing:
        existing.content = req.content[:4000]
        existing.kind = req.kind[:40]
        existing.source = req.source[:80]
        mem_id = existing.id
    else:
        m = CopilotMemory(tenant_id=scope.tenant_id, project_id=scope.project_id if req.project_scoped else None,
                          kind=req.kind[:40], title=req.title.strip()[:200], content=req.content[:4000], source=req.source[:80])
        db.add(m)
        await db.flush()
        mem_id = m.id
    await db.commit()
    return {"id": mem_id}


@router.delete("/memory/{mem_id}")
async def delete_memory(mem_id: str, scope: Scope = Depends(resolve_scope), db: AsyncSession = Depends(get_db)) -> dict:
    m = (await db.execute(select(CopilotMemory).where(
        CopilotMemory.id == mem_id, CopilotMemory.tenant_id == scope.tenant_id))).scalar_one_or_none()
    if m is None:
        raise HTTPException(status_code=404, detail="memory not found")
    await db.delete(m)
    await db.commit()
    return {"deleted": mem_id}


@router.delete("/memory")
async def reset_memory(project_only: bool = False, scope: Scope = Depends(resolve_scope),
                       db: AsyncSession = Depends(get_db)) -> dict:
    """Delete gathered memory — the escape hatch when stale context causes problems.
    Default wipes ALL tenant memory; project_only=true keeps tenant-wide memory."""
    stmt = sa_delete(CopilotMemory).where(CopilotMemory.tenant_id == scope.tenant_id)
    if project_only:
        stmt = stmt.where(CopilotMemory.project_id == scope.project_id)
    res = await db.execute(stmt)
    await db.commit()
    return {"deleted": res.rowcount or 0}

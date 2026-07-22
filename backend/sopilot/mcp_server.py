"""SOPilot MCP server surface (D-11) — the "one entry tool, routed by us".

A minimal Streamable-HTTP MCP server that lets an external voice/chat agent
(e.g. PolarTie's voice agent) consult SOPilot per turn. It exposes a single
tool, ``sop_guidance``: the agent calls it each turn with the caller's latest
message and gets back SOPilot's stage steering (the ``turn.prompt_text``), which
the agent then follows in its own voice.

First cut, on purpose: this wraps the running SOPilot HTTP API (:8100) rather
than importing the runtime in-process, so "add the server and it works". The
low-latency in-process variant (mount inside ``sopilot.api.app`` sharing
``app.state``) is a later optimisation — see the integration doc.

Session mapping: one SOPilot session per MCP connection. On the first tool call
we ``POST /sessions`` (empty ``sop_id`` → the D-11 intake router picks the SOP
from the first utterance), then every turn ``POST /sessions/{id}/converse`` and
return the steering text.

Run (reads config from env):
    SOPILOT_API_KEY=sop_...  SOPILOT_PROJECT=malaga \
        python -m sopilot.mcp_server

Env:
    SOPILOT_API_KEY   (required) tenant Bearer key
    SOPILOT_PROJECT   (required) X-Project slug (AENA = "malaga")
    SOPILOT_BASE_URL  SOPilot HTTP API base (default http://127.0.0.1:8100)
    SOPILOT_SOP_ID    bind a specific SOP; default "" = intake router
    SOPILOT_CHANNEL   session channel (default realtime_voice)
    SOPILOT_SUBSYSTEMS  "" | sop | retrieval | both | advisory (default advisory)
    SOPILOT_MCP_HOST/PORT/PATH  bind (default 127.0.0.1 / 8140 / /mcp)
    SOPILOT_MCP_TRANSPORT  fastmcp transport (default http = Streamable HTTP)
"""
from __future__ import annotations

import os

import httpx
from fastmcp import Context, FastMCP

BASE_URL = os.environ.get("SOPILOT_BASE_URL", "http://127.0.0.1:8100").rstrip("/")
API_KEY = os.environ.get("SOPILOT_API_KEY", "")
PROJECT = os.environ.get("SOPILOT_PROJECT", "")
SOP_ID = os.environ.get("SOPILOT_SOP_ID", "")
CHANNEL = os.environ.get("SOPILOT_CHANNEL", "realtime_voice")
SUBSYSTEMS = os.environ.get("SOPILOT_SUBSYSTEMS", "advisory")
# Which tools to expose: "both" (default), "tool" (model-driven sop_guidance only),
# or "supervisor" (the reserved polartie_ai_agent_supervisor only — for the
# generic PolarTie supervisor extension, so the model never sees a tool to call).
MODE = os.environ.get("SOPILOT_MCP_MODE", "both").strip()

mcp = FastMCP("SOPilot")

# one SOPilot session id per MCP connection (keyed by the MCP session)
_sessions: "dict[str, str]" = {}


def _headers() -> "dict[str, str]":
    return {
        "Authorization": f"Bearer {API_KEY}",
        "X-Project": PROJECT,
        "Content-Type": "application/json",
    }


def _conn_key(ctx: "Context | None") -> str:
    """Stable per-connection key; falls back to a single shared session."""
    for attr in ("session_id", "client_id", "request_id"):
        val = getattr(ctx, attr, None)
        if val:
            return str(val)
    return "default"


async def _ensure_session(client: httpx.AsyncClient, key: str) -> str:
    sid = _sessions.get(key)
    if sid:
        return sid
    r = await client.post(
        f"{BASE_URL}/sessions",
        headers=_headers(),
        json={"sop_id": SOP_ID, "channel": CHANNEL, "subsystems": SUBSYSTEMS},
    )
    r.raise_for_status()
    sid = r.json()["session_id"]
    _sessions[key] = sid
    return sid


# --- in-process mode (mounted inside the SOPilot API app) --------------------
# When mounted via attach_app(), the tool calls the runtime directly using the
# host app's live state (pool/prefetch/embedder/redis) instead of HTTP — no
# separate process, no localhost hop. Single-tenant, from SOPILOT_API_KEY/PROJECT.
_APP = None
_INPROC = False
_SCOPE = None


def attach_app(app) -> None:
    global _APP, _INPROC
    _APP = app
    _INPROC = True


async def _inproc_scope(db):
    global _SCOPE
    if _SCOPE is not None:
        return _SCOPE
    from sqlalchemy import select

    from .models import ApiKey, Project, Tenant
    from .tenancy import Scope, hash_api_key
    row = (await db.execute(
        select(ApiKey, Tenant).join(Tenant, Tenant.id == ApiKey.tenant_id)
        .where(ApiKey.key_hash == hash_api_key(API_KEY), ApiKey.revoked_at.is_(None))
    )).first()
    if row is None:
        raise RuntimeError("SOPilot MCP in-process: API key not found")
    tenant = row[1]
    proj = (await db.execute(
        select(Project).where(Project.tenant_id == tenant.id, Project.slug == PROJECT)
    )).scalar_one_or_none()
    if proj is None:
        raise RuntimeError(f"SOPilot MCP in-process: project '{PROJECT}' not found")
    _SCOPE = Scope(tenant_id=tenant.id, project_id=proj.id, subsystems=(proj.subsystems or "both"))
    return _SCOPE


async def _guidance_inproc(user_message: str, key: str) -> dict:
    from types import SimpleNamespace

    from .api.runtime import ConverseRequest, converse
    from .api.sessions import start_session
    from .db import get_sessionmaker
    from .schemas import SessionStartRequest
    sm = get_sessionmaker()
    async with sm() as db:
        scope = await _inproc_scope(db)
        sid = _sessions.get(key)
        if not sid:
            resp = await start_session(
                SessionStartRequest(sop_id=SOP_ID, channel=CHANNEL, subsystems=SUBSYSTEMS), scope, db)
            sid = resp.session_id
            _sessions[key] = sid
        # converse reads request.app.state.* and writes request.state.query_emb
        req = SimpleNamespace(app=SimpleNamespace(state=_APP.state), state=SimpleNamespace())
        return await converse(
            sid, ConverseRequest(user_message=user_message, steer_only=True), req, scope, db)


async def _guidance(user_message: str, ctx: "Context | None", prev_assistant_message: str = "") -> str:
    """Shared core: route/track the SOPilot session for this connection and return
    the per-turn stage steering. Used by both the model-driven tool (sop_guidance)
    and the auto-driven supervisor extension tool (polartie_ai_agent_supervisor).
    prev_assistant_message is accepted for forward-compat (voice-turn steering)."""
    if not API_KEY or not PROJECT:
        raise RuntimeError("SOPilot MCP server misconfigured: set SOPILOT_API_KEY and SOPILOT_PROJECT")

    key = _conn_key(ctx)
    try:
        if _INPROC:
            data = await _guidance_inproc(user_message, key)
        else:
            async with httpx.AsyncClient(timeout=30.0) as client:
                sid = await _ensure_session(client, key)
                r = await client.post(
                    f"{BASE_URL}/sessions/{sid}/converse",
                    headers=_headers(),
                    # steer_only: we only use turn.prompt_text — skip SOPilot's own
                    # responder LLM call (routing/switch/tracking/pool still run).
                    json={"user_message": user_message, "steer_only": True},
                )
                r.raise_for_status()
                data = r.json()
    except Exception:
        # Graceful degradation: a supervisor hiccup (backend error, timeout,
        # post-terminal turn) must NEVER break the live call. Drop the cached
        # session so the next turn re-routes cleanly, and return benign guidance.
        _sessions.pop(key, None)
        return "Continue assisting the caller naturally and courteously."

    turn = data.get("turn") or {}
    guidance = (turn.get("prompt_text") or turn.get("context_block") or data.get("reply") or "").strip()
    routing = data.get("routing") or {}
    if routing.get("sop_id") and turn.get("prompt_text"):
        # a compact routed-stage header helps the model trust the guidance
        stage = turn.get("chosen_action") or ""
        guidance = f"[SOP stage: {stage}]\n{guidance}" if stage else guidance
    if data.get("terminal"):
        guidance = (guidance + "\n[The procedure is complete — close the conversation politely.]").strip()
    return guidance or "No specific guidance for this turn; respond naturally and helpfully."


async def sop_guidance(user_message: str, ctx: Context = None) -> str:
    """Get SOPilot's guidance for the current turn of a customer-service call.

    Call this every turn with the caller's latest message. Returns the standard
    operating procedure's stage guidance for how to handle this turn — the step
    to take, what to say, and any must-say / must-not-say constraints. Follow the
    returned guidance in your own words; do not read it out verbatim.
    """
    return await _guidance(user_message, ctx)


async def polartie_ai_agent_supervisor(
    user_message: str, prev_assistant_message: str = "", ctx: Context = None
) -> str:
    """[PolarTie supervisor extension — reserved tool] Per-turn SOP steering.

    When a PolarTie voice agent discovers this reserved tool on a connected MCP
    server, it treats the server as a supervisor: it switches to client-driven
    turns and calls this tool every turn with the caller's latest transcript,
    then applies the returned text as session.update instructions before letting
    the model respond. Returns the SOP stage steering for this turn (SOP chosen
    by SOPilot's intake router). The agent should follow it in its own voice.
    """
    return await _guidance(user_message, ctx, prev_assistant_message)


# Register tools per mode (see MODE above).
if MODE in ("both", "tool"):
    mcp.tool(sop_guidance)
if MODE in ("both", "supervisor"):
    mcp.tool(polartie_ai_agent_supervisor)


def main() -> None:
    host = os.environ.get("SOPILOT_MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("SOPILOT_MCP_PORT", "8140"))
    path = os.environ.get("SOPILOT_MCP_PATH", "/mcp")
    transport = os.environ.get("SOPILOT_MCP_TRANSPORT", "http")
    mcp.run(transport=transport, host=host, port=port, path=path)


if __name__ == "__main__":
    main()

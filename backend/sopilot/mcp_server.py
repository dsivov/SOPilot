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


@mcp.tool
async def sop_guidance(user_message: str, ctx: Context = None) -> str:
    """Get SOPilot's guidance for the current turn of a customer-service call.

    Call this every turn with the caller's latest message. Returns the standard
    operating procedure's stage guidance for how to handle this turn — the step
    to take, what to say, and any must-say / must-not-say constraints. Follow the
    returned guidance in your own words; do not read it out verbatim.
    """
    if not API_KEY or not PROJECT:
        raise RuntimeError("SOPilot MCP server misconfigured: set SOPILOT_API_KEY and SOPILOT_PROJECT")

    key = _conn_key(ctx)
    async with httpx.AsyncClient(timeout=30.0) as client:
        sid = await _ensure_session(client, key)
        r = await client.post(
            f"{BASE_URL}/sessions/{sid}/converse",
            headers=_headers(),
            json={"user_message": user_message},
        )
        r.raise_for_status()
        data = r.json()

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


def main() -> None:
    host = os.environ.get("SOPILOT_MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("SOPILOT_MCP_PORT", "8140"))
    path = os.environ.get("SOPILOT_MCP_PATH", "/mcp")
    transport = os.environ.get("SOPILOT_MCP_TRANSPORT", "http")
    mcp.run(transport=transport, host=host, port=port, path=path)


if __name__ == "__main__":
    main()

"""MCP client + fetcher: JSON-RPC flow over httpx MockTransport (no network),
plus secrets encryption round-trip."""
import json

import httpx
import pytest

from sopilot.mcp_client import McpClient, McpError


def make_transport(tool_result: dict, *, sse: bool = False, capture: list | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if capture is not None:
            capture.append((body, dict(request.headers)))
        method = body.get("method")
        if method == "initialize":
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": "2025-03-26"}},
                headers={"mcp-session-id": "sess-42"},
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/call":
            msg = {"jsonrpc": "2.0", "id": body["id"], "result": tool_result}
            if sse:
                text = f"event: message\ndata: {json.dumps(msg)}\n\n"
                return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})
            return httpx.Response(200, json=msg)
        return httpx.Response(400)

    return httpx.MockTransport(handler)


RESULT = {"content": [{"type": "text", "text": "policy #777: premium 512/yr"}], "isError": False}


async def test_call_tool_json_flow():
    capture: list = []
    client = McpClient("http://mcp.test/rpc", transport=make_transport(RESULT, capture=capture))
    text = await client.call_tool("kb_search", {"query": "renewal premium"})
    assert text == "policy #777: premium 512/yr"
    methods = [b["method"] for b, _ in capture]
    assert methods == ["initialize", "notifications/initialized", "tools/call"]
    # session id from initialize is carried on subsequent calls
    assert capture[2][1].get("mcp-session-id") == "sess-42"
    assert capture[2][0]["params"] == {"name": "kb_search", "arguments": {"query": "renewal premium"}}


async def test_call_tool_sse_flow():
    client = McpClient("http://mcp.test/rpc", transport=make_transport(RESULT, sse=True))
    assert await client.call_tool("kb_search", {"query": "x"}) == "policy #777: premium 512/yr"


async def test_tool_error_raises():
    err = {"content": [{"type": "text", "text": "boom"}], "isError": True}
    client = McpClient("http://mcp.test/rpc", transport=make_transport(err))
    with pytest.raises(McpError):
        await client.call_tool("kb_search", {})


def test_secret_encrypt_roundtrip():
    from sopilot.secrets import decrypt, encrypt

    token = encrypt("kr-live-key-123")
    assert token != "kr-live-key-123"
    assert decrypt(token) == "kr-live-key-123"


async def test_mcp_fetcher_missing_secret_degrades(scope_a):
    """A missing tenant secret must produce a self-explaining summary, not a crash."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from sopilot.fetchers.mcp import McpFetcher
    from sopilot.models import Base
    from sopilot.schemas import DataDependency

    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        # only the tables sqlite can handle; tenant_secrets is vector-free so it works
        await conn.run_sync(
            lambda sync: Base.metadata.tables["tenant_secrets"].create(sync)
        )
    sm = async_sessionmaker(engine, expire_on_commit=False)
    dep = DataDependency(
        name="kb",
        kind="mcp",
        config={"server": "http://mcp.test/rpc", "tool": "search", "auth_secret": "kr_key"},
    )
    out = await McpFetcher(sm).fetch(dep, scope=scope_a, session_id="s1", action_name="A", query="q")
    assert "secret 'kr_key' not set" in out.summary
    await engine.dispose()

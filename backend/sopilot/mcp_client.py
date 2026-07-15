"""Minimal MCP client (Streamable HTTP transport): initialize → tools/call.

Deliberately small — the fetcher SDK needs exactly one verb (call a tool, get
text back). Handles both plain-JSON and SSE-framed responses, and carries the
Mcp-Session-Id header when the server issues one.
"""
from __future__ import annotations

import json
from typing import Any

import httpx

PROTOCOL_VERSION = "2025-03-26"


class McpError(RuntimeError):
    pass


def _parse_body(res: httpx.Response) -> dict | None:
    """JSON-RPC message from either a JSON or an SSE (text/event-stream) body."""
    ctype = res.headers.get("content-type", "")
    if "text/event-stream" in ctype:
        message: dict | None = None
        for line in res.text.splitlines():
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload:
                    try:
                        candidate = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(candidate, dict) and ("result" in candidate or "error" in candidate):
                        message = candidate
        return message
    if not res.content:
        return None
    return res.json()


class McpClient:
    def __init__(self, url: str, headers: dict[str, str] | None = None, *, transport: httpx.AsyncBaseTransport | None = None):
        self.url = url
        self.extra_headers = headers or {}
        self.session_id: str | None = None
        self._transport = transport
        self._next_id = 0

    def _headers(self) -> dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self.extra_headers,
        }
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    async def _rpc(self, client: httpx.AsyncClient, method: str, params: dict | None, *, notification: bool = False) -> dict | None:
        body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notification:
            self._next_id += 1
            body["id"] = self._next_id
        res = await client.post(self.url, json=body, headers=self._headers(), timeout=30.0)
        if res.status_code >= 400:
            raise McpError(f"{method}: HTTP {res.status_code} {res.text[:200]}")
        if sid := res.headers.get("mcp-session-id"):
            self.session_id = sid
        if notification:
            return None
        message = _parse_body(res)
        if message is None:
            raise McpError(f"{method}: empty response")
        if "error" in message:
            raise McpError(f"{method}: {message['error']}")
        return message.get("result")

    async def call_tool(self, tool: str, arguments: dict) -> str:
        """initialize → initialized → tools/call; returns concatenated text content."""
        async with httpx.AsyncClient(transport=self._transport) as client:
            await self._rpc(
                client,
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "sopilot-fetcher", "version": "0.1"},
                },
            )
            await self._rpc(client, "notifications/initialized", None, notification=True)
            result = await self._rpc(client, "tools/call", {"name": tool, "arguments": arguments})
            if result is None:
                raise McpError("tools/call: no result")
            if result.get("isError"):
                raise McpError(f"tool '{tool}' returned an error: {str(result)[:200]}")
            parts: list[str] = []
            for item in result.get("content", []):
                if item.get("type") == "text":
                    parts.append(item.get("text", ""))
            return "\n".join(p for p in parts if p).strip()

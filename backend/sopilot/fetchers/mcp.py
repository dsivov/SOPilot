"""MCP fetcher — calls a tool on a tenant-configured MCP server (Streamable HTTP).

Dependency config:
    server:       MCP endpoint URL (required)
    tool:         tool name to call (required)
    args:         static argument template (dict)
    query_arg:    which argument receives the rendered query (default "query")
    auth_secret:  tenant-secret NAME whose value becomes the auth header value
    auth_header:  header to carry it in (default "X-API-Key"; use
                  "Authorization" with a "Bearer ..." value stored in the secret)

Idempotency remains the author's declaration: only mark MCP deps idempotent when
the tool is a pure read — the prefetch scheduler trusts the flag.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import async_sessionmaker

from ..mcp_client import McpClient
from ..schemas import DataDependency
from ..tenancy import Scope
from .base import BaseFetcher, FetchOutcome


class McpFetcher(BaseFetcher):
    def __init__(self, sessionmaker: async_sessionmaker):
        self.sessionmaker = sessionmaker

    async def fetch(
        self,
        dep: DataDependency,
        *,
        scope: Scope,
        session_id: str,
        action_name: str,
        query: str | None = None,
    ) -> FetchOutcome:
        cfg = dep.config or {}
        server = cfg.get("server") or ""
        tool = cfg.get("tool") or ""
        if not server or not tool:
            return FetchOutcome(payload=None, summary=f"<mcp: dep '{dep.name}' missing server/tool config>")

        headers: dict[str, str] = {}
        secret_name = cfg.get("auth_secret") or ""
        if secret_name:
            from ..secrets import get_secret

            async with self.sessionmaker() as db:
                value = await get_secret(db, scope.tenant_id, secret_name)
            if value is None:
                return FetchOutcome(
                    payload=None,
                    summary=f"<mcp: tenant secret '{secret_name}' not set — configure it in /secrets>",
                )
            headers[cfg.get("auth_header") or "X-API-Key"] = value

        arguments = dict(cfg.get("args") or {})
        if query:
            arguments[cfg.get("query_arg") or "query"] = query

        client = McpClient(server, headers=headers)
        text = await client.call_tool(tool, arguments)
        summary = f"MCP {tool}@{dep.name}: {text[:160]}" if text else f"MCP {tool}@{dep.name}: (empty)"
        return FetchOutcome(payload=text, summary=summary[:200])

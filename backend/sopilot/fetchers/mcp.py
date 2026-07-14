"""MCP fetcher — P0 stub with the production interface locked.

Dependency config keys (the P2 connector will honour these):
    server:  MCP server name/URL registered for the project
    tool:    tool name to call
    args:    static argument template (query lands in args["query"])
"""
from __future__ import annotations

from ..schemas import DataDependency
from ..tenancy import Scope
from .base import BaseFetcher, FetchOutcome


class McpFetcher(BaseFetcher):
    async def fetch(
        self,
        dep: DataDependency,
        *,
        scope: Scope,
        session_id: str,
        action_name: str,
        query: str | None = None,
    ) -> FetchOutcome:
        raise NotImplementedError(
            f"MCP fetcher not wired yet (dep '{dep.name}' wants server="
            f"{(dep.config or {}).get('server')!r}, tool={(dep.config or {}).get('tool')!r})"
        )

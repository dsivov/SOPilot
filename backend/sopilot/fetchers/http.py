"""Generic HTTP retrieval fetcher — RAG endpoints, search APIs, internal tools.

Dependency/connector config:
    url:           endpoint (required)
    method:        GET | POST (default POST)
    query_field:   body/param key that receives the rendered query (default "query")
    body:          static JSON body template merged under the query (POST)
    params:        static query params (GET, or extra params on POST)
    headers:       static headers
    auth_secret:   tenant-secret NAME whose value becomes the auth header value
    auth_header:   header carrying it (default "Authorization" — store the full
                   "Bearer …" value in the secret; use "X-API-Key" style otherwise)
    result_path:   dot-path into the response JSON to the payload of interest
                   (e.g. "results" or "data.hits"); default: whole body
    timeout_s:     request timeout (default 15)

Side-effect freedom is the author's declaration, as everywhere in the SDK:
only mark HTTP deps idempotent when the endpoint is a pure read.
"""
from __future__ import annotations

import json

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker

from ..schemas import DataDependency
from ..tenancy import Scope
from .base import BaseFetcher, FetchOutcome


def _dig(obj: object, path: str) -> object:
    for part in [p for p in path.split(".") if p]:
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, list) and part.isdigit():
            obj = obj[int(part)] if int(part) < len(obj) else None
        else:
            return None
    return obj


class HttpFetcher(BaseFetcher):
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
        url = cfg.get("url") or ""
        if not url:
            return FetchOutcome(payload=None, summary=f"<http: dep '{dep.name}' has no url configured>")

        headers: dict[str, str] = {str(k): str(v) for k, v in (cfg.get("headers") or {}).items()}
        secret_name = cfg.get("auth_secret") or ""
        if secret_name:
            from ..secrets import get_secret

            async with self.sessionmaker() as db:
                value = await get_secret(db, scope.tenant_id, secret_name)
            if value is None:
                return FetchOutcome(
                    payload=None, summary=f"<http: tenant secret '{secret_name}' not found for {dep.name}>"
                )
            headers[cfg.get("auth_header") or "Authorization"] = value

        method = str(cfg.get("method") or "POST").upper()
        query_field = cfg.get("query_field") or "query"
        params = {str(k): v for k, v in (cfg.get("params") or {}).items()}
        timeout = float(cfg.get("timeout_s") or 15)

        async with httpx.AsyncClient(timeout=timeout) as client:
            if method == "GET":
                if query is not None:
                    params[query_field] = query
                resp = await client.get(url, params=params, headers=headers)
            else:
                body = dict(cfg.get("body") or {})
                if query is not None:
                    body[query_field] = query
                resp = await client.request(method, url, json=body, params=params or None, headers=headers)
        resp.raise_for_status()

        try:
            data: object = resp.json()
        except json.JSONDecodeError:
            data = resp.text

        payload = _dig(data, cfg["result_path"]) if cfg.get("result_path") and isinstance(data, (dict, list)) else data
        text = json.dumps(payload, ensure_ascii=False, default=str) if not isinstance(payload, str) else payload
        return FetchOutcome(payload=payload, summary=text[:200])

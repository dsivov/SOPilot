from __future__ import annotations

from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI

from ..config import get_settings
from ..db import get_sessionmaker
from ..embeddings import OpenAIEmbeddings
from ..fetchers import MockFetcher, PgVectorRagFetcher, register_fetcher
from ..fetchers.mcp import McpFetcher
from ..pool import SessionPool
from ..prefetch import PrefetchManager
from . import admin, sessions, sops


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings = get_settings()
        redis = aioredis.from_url(settings.redis_url)
        embedder = OpenAIEmbeddings()
        pool = SessionPool(redis)
        app.state.redis = redis
        app.state.embedder = embedder
        app.state.pool = pool
        app.state.prefetch = PrefetchManager(pool, get_sessionmaker(), embedder)
        register_fetcher("mock", MockFetcher())
        register_fetcher("rag", PgVectorRagFetcher(get_sessionmaker(), embedder))
        register_fetcher("mcp", McpFetcher())
        for kind in ("kg", "db", "api"):
            register_fetcher(kind, MockFetcher())  # real connectors land with the fetcher SDK P2 work
        yield
        await redis.aclose()

    app = FastAPI(title="SOPilot", version="0.1.0", lifespan=lifespan)
    app.include_router(admin.router)
    app.include_router(sops.router)
    app.include_router(sessions.router)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "service": "sopilot", "version": "0.1.0"}

    return app


app = create_app()

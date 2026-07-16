from __future__ import annotations

from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI

from ..config import get_settings
from ..db import get_sessionmaker
from ..embeddings import OpenAIEmbeddings
from ..fetchers import register_default_fetchers
from ..pool import SessionPool
from ..prefetch import PrefetchManager
from ..supervisor import SupervisorWorker
from . import abtests, admin, connectors, metrics, prompt_blocks, runtime, secrets, sessions, sops, traces, voice


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        import asyncio

        settings = get_settings()
        redis = aioredis.from_url(settings.redis_url)
        embedder = OpenAIEmbeddings()
        pool = SessionPool(redis)
        app.state.redis = redis
        app.state.embedder = embedder
        app.state.pool = pool
        app.state.prefetch = PrefetchManager(pool, get_sessionmaker(), embedder)
        register_default_fetchers(get_sessionmaker(), embedder)
        # D-1 dev mode: run a supervisor consumer in-process (production runs
        # `sopilot-supervisor` as its own deployment — same code path).
        worker = None
        worker_task = None
        if settings.embedded_supervisor:
            worker = SupervisorWorker(redis, embedder=embedder, consumer_name="embedded")
            worker_task = asyncio.create_task(worker.run())
        yield
        if worker is not None:
            worker.stop()
            if worker_task is not None:
                await worker_task
        await redis.aclose()

    app = FastAPI(title="SOPilot", version="0.1.0", lifespan=lifespan)
    app.include_router(admin.router)
    app.include_router(sops.router)
    app.include_router(sessions.router)
    app.include_router(runtime.router)
    app.include_router(prompt_blocks.router)
    app.include_router(voice.router)
    app.include_router(metrics.router)
    app.include_router(secrets.router)
    app.include_router(traces.router)
    app.include_router(abtests.router)
    app.include_router(connectors.router)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "service": "sopilot", "version": "0.1.0"}

    return app


app = create_app()

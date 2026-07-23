from __future__ import annotations

import logging
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
from . import abtests, admin, connectors, corpora, metrics, project_io, prompt_blocks, runtime, secrets, sessions, sops, traces, voice


def create_app() -> FastAPI:
    # App-logger output (turn planned / fetch ok lines): uvicorn configures only
    # its own loggers, so give the root logger a handler if nobody has yet.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    # D-11: optionally mount the MCP surface (/mcp) in-process (shares app.state).
    _mcp_app = None
    if get_settings().mcp_mount:
        from ..mcp_server import mcp as _mcp
        # internal path "/" + mount at "/mcp" → MCP served at /mcp
        _mcp_app = _mcp.http_app(path="/")

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
        if _mcp_app is not None:
            # run the mounted MCP app's session-manager lifespan alongside ours,
            # and switch the tool to in-process calls (shared app.state)
            from ..mcp_server import attach_app
            async with _mcp_app.lifespan(app):
                attach_app(app)
                yield
        else:
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
    app.include_router(corpora.router)
    app.include_router(project_io.router)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "service": "sopilot", "version": "0.1.0"}

    if _mcp_app is not None:
        app.mount("/mcp", _mcp_app)

    return app


app = create_app()


def main() -> None:
    """Production entrypoint (`sopilot-api`): host/port from the environment —
    SOPILOT_HOST (default 127.0.0.1; set 0.0.0.0 behind a reverse proxy) and
    SOPILOT_PORT (default 8100)."""
    import os

    import uvicorn

    uvicorn.run(
        "sopilot.api.app:app",
        host=os.environ.get("SOPILOT_HOST", "127.0.0.1"),
        port=int(os.environ.get("SOPILOT_PORT", "8100")),
    )

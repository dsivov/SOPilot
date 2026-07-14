"""Latency-simulating stub — used by benchmarks and as the dev default for kinds
whose real connector isn't configured yet.
"""
from __future__ import annotations

import asyncio
import random

from ..schemas import DataDependency
from ..tenancy import Scope
from .base import BaseFetcher, FetchOutcome


class MockFetcher(BaseFetcher):
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
        base_ms = max(0, int(dep.expected_latency_ms or 0))
        jitter = int(cfg.get("jitter_ms", 0) or 0)
        sleep_ms = base_ms + (random.randint(-jitter, jitter) if jitter else 0)
        await asyncio.sleep(max(0.0, sleep_ms / 1000.0))
        text = cfg.get("text") or f"<mock data for {dep.name} (action={action_name})>"
        return FetchOutcome(payload=text, summary=str(text)[:200])

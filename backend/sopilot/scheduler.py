"""PASTE-style speculative-work scheduler — bounded budget + critical-path preemption.

Ported from the POC. Per-worker state (the contended resource is this worker's LLM
API concurrency): at most `speculative_budget` speculative calls at once, and new
speculative calls hold while any critical-path call is in flight. Speculative-ness
propagates through task trees via a ContextVar.
"""
from __future__ import annotations

import asyncio
import contextvars

from .config import get_settings

speculative_mode: contextvars.ContextVar[bool] = contextvars.ContextVar("speculative_mode", default=False)


def is_speculative() -> bool:
    return speculative_mode.get()


class _State:
    def __init__(self) -> None:
        self._sem: asyncio.Semaphore | None = None
        self._idle: asyncio.Event | None = None
        self._critical_active = 0
        self._lock: asyncio.Lock | None = None
        self.spec_started = 0
        self.spec_held_for_critical = 0

    def _ensure(self) -> None:
        if self._sem is None:
            self._sem = asyncio.Semaphore(max(1, get_settings().speculative_budget))
            self._idle = asyncio.Event()
            self._idle.set()
            self._lock = asyncio.Lock()

    async def enter_critical(self) -> None:
        self._ensure()
        async with self._lock:
            self._critical_active += 1
            self._idle.clear()

    async def exit_critical(self) -> None:
        async with self._lock:
            self._critical_active -= 1
            if self._critical_active <= 0:
                self._critical_active = 0
                self._idle.set()

    async def acquire_speculative(self) -> None:
        self._ensure()
        if not self._idle.is_set():
            self.spec_held_for_critical += 1
        await self._idle.wait()
        await self._sem.acquire()
        # A critical op may have slipped in between wait and acquire — yield and retry
        # so the live path stays strictly ahead.
        while not self._idle.is_set():
            self._sem.release()
            await self._idle.wait()
            await self._sem.acquire()
        self.spec_started += 1

    def release_speculative(self) -> None:
        self._sem.release()


_state = _State()


class critical_path:
    async def __aenter__(self) -> "critical_path":
        await _state.enter_critical()
        return self

    async def __aexit__(self, *exc) -> None:
        await _state.exit_critical()


class speculative_slot:
    async def __aenter__(self) -> "speculative_slot":
        await _state.acquire_speculative()
        return self

    async def __aexit__(self, *exc) -> None:
        _state.release_speculative()


def stats() -> dict:
    return {
        "budget": get_settings().speculative_budget,
        "spec_started": _state.spec_started,
        "spec_held_for_critical": _state.spec_held_for_critical,
    }

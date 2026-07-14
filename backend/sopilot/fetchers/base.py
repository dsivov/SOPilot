"""Fetcher SDK. One fetcher per DataDependency.kind; all fetchers must be
side-effect free — the prefetch scheduler only ever fires idempotent deps
speculatively, and that invariant is enforced at schedule time, not here.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..schemas import DataDependency
from ..tenancy import Scope


@dataclass
class FetchOutcome:
    payload: object
    summary: str  # ≤200 chars; what the rerank embeds and the prompt block shows


class BaseFetcher(ABC):
    @abstractmethod
    async def fetch(
        self,
        dep: DataDependency,
        *,
        scope: Scope,
        session_id: str,
        action_name: str,
        query: str | None = None,
    ) -> FetchOutcome: ...


_REGISTRY: dict[str, BaseFetcher] = {}


def register_fetcher(kind: str, fetcher: BaseFetcher) -> None:
    _REGISTRY[kind] = fetcher


def get_fetcher(kind: str) -> BaseFetcher:
    fetcher = _REGISTRY.get(kind)
    if fetcher is None:
        raise KeyError(f"no fetcher registered for kind '{kind}'")
    return fetcher

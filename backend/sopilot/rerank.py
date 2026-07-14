"""Per-turn pool curation — v3 semantics from the POC, verbatim where it counts:
cosine-rank against the live user message, two-layer dedup (key-equality on
(dependency, summary prefix) then pairwise cosine ≥ 0.95 — a thresholded MMR),
take top-3. No LLM anywhere on this path.

Validated numbers this preserves: mean ~300 ms (dominated by one query-embedding
call), quality parity with the retired LLM rerank (87% pick overlap + dedup).
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np

from .embeddings import EmbeddingProvider, cosine
from .pool import PoolItem

MAX_PREFILTER_CANDIDATES = 8
DEDUP_PREFIX_LEN = 60
DEDUP_COSINE_THRESHOLD = 0.95


@dataclass
class RerankResult:
    picks: list[PoolItem]
    rationale: str
    duration_ms: int


def _scored(pool: list[PoolItem], query_emb: np.ndarray | None) -> list[tuple[float, PoolItem]]:
    """Data items only, cosine-desc; items without embeddings trail in pool order."""
    data_items = [p for p in pool if p.kind == "data"]
    if query_emb is None:
        return [(-1.0, p) for p in data_items]
    ranked = [(cosine(query_emb, p.summary_embedding), p) for p in data_items if p.summary_embedding is not None]
    ranked.sort(key=lambda x: -x[0])
    unranked = [(-1.0, p) for p in data_items if p.summary_embedding is None]
    return ranked + unranked


def _dedup_take_top_k(scored: list[tuple[float, PoolItem]], max_picks: int) -> list[tuple[float, PoolItem]]:
    picks: list[tuple[float, PoolItem]] = []
    pick_keys: set[tuple[str, str]] = set()
    pick_embs: list[np.ndarray] = []
    for cos, item in scored:
        key = (item.dependency_name, (item.payload_summary or "")[:DEDUP_PREFIX_LEN].strip())
        if key in pick_keys:
            continue
        emb = item.summary_embedding
        if emb is not None and any(cosine(emb, prev) >= DEDUP_COSINE_THRESHOLD for prev in pick_embs):
            continue
        picks.append((cos, item))
        pick_keys.add(key)
        if emb is not None:
            pick_embs.append(emb)
        if len(picks) >= max_picks:
            break
    return picks


async def rerank_pool_for_turn(
    pool: list[PoolItem],
    *,
    live_user_message: str,
    embedder: EmbeddingProvider,
    max_picks: int = 3,
) -> RerankResult:
    if not pool:
        return RerankResult([], "", 0)
    t0 = time.perf_counter()
    query_emb: np.ndarray | None = None
    if live_user_message.strip():
        try:
            query_emb = await embedder.embed(live_user_message)
        except Exception:
            query_emb = None  # degrade to recency order rather than fail the turn
    scored = _scored(pool, query_emb)
    picks_scored = _dedup_take_top_k(scored[:MAX_PREFILTER_CANDIDATES], max_picks)
    duration_ms = int((time.perf_counter() - t0) * 1000)
    if not picks_scored:
        return RerankResult([], "", duration_ms)
    cos_str = ", ".join(f"{c:.2f}" if c >= 0 else "n/a" for c, _ in picks_scored)
    return RerankResult(
        picks=[p for _, p in picks_scored],
        rationale=f"cosine-rank+dedup: top-{len(picks_scored)} cosines=[{cos_str}]",
        duration_ms=duration_ms,
    )


def speculative_context_block(picks: list[PoolItem]) -> str:
    """The honest-framing prompt contract (validated: 'speculative' beats 'curated').

    This wording is a fixed rule of the system, not a tunable — see the framing
    ablation in the research (§6.2): over-confident labels caused the agent to
    force irrelevant items into closing turns.
    """
    if not picks:
        return ""
    lines = [
        "SPECULATIVELY PRE-STAGED CONTEXT (may or may not fit this turn):",
        "The items below were prefetched based on a prediction of where the conversation",
        "was heading. Use only what is relevant to the user's latest message. Ignore what",
        "isn't relevant, and do NOT force irrelevant data into your reply. If nothing fits,",
        "reply naturally without it.",
        "",
    ]
    for p in picks:
        lines.append(f"- [{p.dependency_name} — {p.source_action}] {p.payload_summary}")
    return "\n".join(lines)

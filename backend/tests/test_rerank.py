from datetime import timedelta

from sopilot.embeddings import FakeEmbeddings
from sopilot.pool import PoolItem, utcnow
from sopilot.rerank import rerank_pool_for_turn, speculative_context_block


async def make_pool_items(embedder: FakeEmbeddings) -> list[PoolItem]:
    now = utcnow()

    async def item(dep: str, summary: str, conf: float = 0.5, kind: str = "data") -> PoolItem:
        return PoolItem(
            dependency_name=dep,
            source_action="ActA",
            payload=summary,
            payload_summary=summary,
            confidence=conf,
            fetched_at=now,
            expires_at=now + timedelta(seconds=300),
            kind=kind,
            summary_embedding=await embedder.embed(summary),
        )

    return [
        await item("policy", "policy #INS-882431 renewal terms and premium"),
        await item("policy", "policy #INS-882431 renewal terms and premium"),  # exact dup
        await item("market_rates", "competitor market rates for coverage"),
        await item("claims", "claims history: two claims in 2024"),
        await item("weather", "unrelated weather data for tomorrow"),
        await item("instr", "pre-generated pitch text", kind="instruction"),
    ]


async def test_relevance_ordering(embedder):
    pool = await make_pool_items(embedder)
    res = await rerank_pool_for_turn(
        pool,
        live_user_message="policy #INS-882431 renewal terms and premium",
        embedder=embedder,
        max_picks=3,
    )
    # The literal-match summary must rank first (FakeEmbeddings: same text → same vector).
    assert res.picks[0].dependency_name == "policy"
    assert res.duration_ms >= 0
    assert "cosine-rank+dedup" in res.rationale


async def test_dedup_drops_duplicate(embedder):
    pool = await make_pool_items(embedder)
    res = await rerank_pool_for_turn(
        pool, live_user_message="policy renewal", embedder=embedder, max_picks=3
    )
    summaries = [p.payload_summary for p in res.picks]
    assert len(summaries) == len(set(summaries))  # the exact-dup policy item appears once


async def test_instructions_excluded_from_data_rerank(embedder):
    pool = await make_pool_items(embedder)
    res = await rerank_pool_for_turn(pool, live_user_message="pitch", embedder=embedder, max_picks=5)
    assert all(p.kind == "data" for p in res.picks)


async def test_empty_pool(embedder):
    res = await rerank_pool_for_turn([], live_user_message="hi", embedder=embedder)
    assert res.picks == [] and res.duration_ms == 0


async def test_speculative_framing_contract(embedder):
    pool = await make_pool_items(embedder)
    res = await rerank_pool_for_turn(pool, live_user_message="policy", embedder=embedder)
    block = speculative_context_block(res.picks)
    # The honest-framing ablation result is a fixed contract: these phrases must survive edits.
    assert "SPECULATIVELY PRE-STAGED" in block
    assert "may or may not fit" in block
    assert "reply naturally" in block
    assert speculative_context_block([]) == ""

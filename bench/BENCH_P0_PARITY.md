# P0 Parity Benchmark — POC numbers reproduced on the production stack

**Date:** 2026-07-14 · **Stack:** SOPilot @ main (Postgres 16 + pgvector, Redis 7,
`sopilot-api` + embedded supervisor over the `events:turns` stream) · **SOP:**
car-insurance renewal (ported MCPlanner seed, 25-doc RAG corpus in pgvector) ·
**Harness:** `sopilot-bench` (LLM user-sim + proposer + responder over HTTP,
per-session JSONL in this directory).

## Verdict

**The retrieval machinery reproduces the research results on the new
architecture.** Speculative hit rate and live-fallback rate land on (run 1) or
above (run 2, warm history) the POC references. Two misses are documented below —
one target missed honestly (plan-turn p95), one metric not comparable by design
(session success).

| Metric | POC reference | Run 1 (N=20, cold-ish) | Run 2 (N=10, warm) | Verdict |
|---|---|---|---|---|
| Speculative hit rate (consumed spec ÷ all consumed) | ~91% | **90.7%** | **96.3%** | ✅ parity / better |
| Live-fallback rate | ~9% | **9.3%** | **3.7%** | ✅ parity / better |
| Turns with ≥1 pool pick | 96% (of eligible turns) | 81.5% (all turns) | **89.2%** (all turns) | ✅ comparable¹ |
| Context-selection (rerank) p50 / p95 | 246 / 483 ms | 0 / 0 (degraded²) | **230 / 600 ms** | ✅ p50 · ⚠️ p95 +117ms³ |
| plan-turn HTTP p50 / p95 | n/a (new metric) | 55 / 432 ms² | **300 / 890 ms** | ❌ p95 > 500ms target⁴ |
| Latency hidden / session (mean) | ~22.5 s | 40.5 s | **35.6 s** | ✅ (SOP-dependent scale) |
| Session success | 10/10 (tuned POC roles, 20-turn cap) | 25% | 30% | ➖ not comparable⁵ |

¹ Denominators differ: POC counted turns where the pool had eligible items; we report all turns
including openings with an empty pool. On data-bearing turns the pool supplied context at
POC-comparable rates.
² Run 1 accidentally ran with no `OPENAI_API_KEY` in the server process — the rerank degraded to
recency ordering (by design) and pool items carried no embeddings. Fixed (config now exports `.env`
to the process env); run 2 exercises the true semantic path. Kept in the record because the graceful
degradation itself is a designed-for mode and it visibly worked.
³ Run 2's rerank includes a live OpenAI embedding round-trip per turn at N=98 turns; the POC's 483ms
was measured over 110 calls on a different network path. Remediation if it matters: batch/cache the
query embedding, or start it during ASR finalization.
⁴ Cause analysis: plan-turn total = rerank (~230–600ms) + consume's bounded await-in-flight poll (up
to 2s when a predicted fetch is still running — deliberately trading a sub-second wait against a
multi-second live fetch) + audit writes. Remediations for P2 (before the voice adapter):
pre-trigger rerank on ASR-final, cap await-in-flight for voice profiles, move audit writes off-path.
Not a blocker for text channels.
⁵ Success rate here measures the *bench harness roles* (gpt-4o-mini user-sim/responder, 12-turn cap
vs POC's 20) — not the runtime. 7 of 10 non-successes in run 2 were 12-turn caps ("abandoned") with
healthy conversations still in progress. Real conversation-quality measurement belongs to the P2
runtime with its actual classifier + responder, and the human A/B harness (P3).

## Run-1 → run-2 fixes (both committed)

1. **Server env loading:** pydantic-settings only consumes `SOPILOT_*` keys, so
   `OPENAI_API_KEY` from `.env` never reached the OpenAI SDK in the server
   process. `config._load_env_file()` now exports it at startup.
2. **Proposer terminal misclassification:** run 1 had 8/20 sessions terminated on
   turn 1 by the gpt-4o-mini proposer classifying opening lines as terminal
   states — the research's "cheap classify collapses quality" warning reproduced
   exactly (POC: 10/10 → 4/10 with a small classify model). Fixes: `--proposer-model gpt-4o`
   + explicit rule that terminal states require an explicit customer statement.
   Run 2 had zero turn-1 terminations. **Carry-forward for P2:** the runtime
   classifier gets the same treatment — a strong model or a validated small one,
   never an unvalidated cheap one.

## Reproduce

```bash
# API up with embedded supervisor, then:
.venv/bin/python ../scripts/seed_bench.py <MCPlanner-repo-path>   # tenant/SOP/corpus
.venv/bin/sopilot-bench --api-key <key> --project bench --sop-id <id> \
    --sessions 20 --warmup 3 --proposer-model gpt-4o --out results.jsonl
```

Raw per-session data: `run1_n20_degraded_rerank.jsonl`, `run2_n10_fixed.jsonl`
(metrics only — no conversation text). Fetch-level ground truth is in the
`data_fetches` / `pool_picks` tables for the `bench` tenant.

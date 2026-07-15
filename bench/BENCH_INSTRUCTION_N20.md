# Instruction pre-generation — N=20 measurement (the pre-committed claim gate)

**Date:** 2026-07-15 · **Runner:** `sopilot-bench --mode converse` (the bench only
simulates the caller; classification, planning, drafting, and serving all happen
in the real runtime) · **SOP:** Appointment Scheduling & Triage (fresh `mbbench`
tenant, zero prior history) · **Users:** 10 rotating personas (impatient, elderly,
price-sensitive, reschedule, urgent-symptoms, chatty, skeptical, proxy caller,
cooperative, undecided) · 5 warm-up + 20 measured sessions, 12-turn cap.

## Verdict against the pre-committed criteria

| Criterion | Result | Verdict |
|---|---|---|
| Instruction hit rate ≥ 70% (eligible turns) | warm-up **44.4%** → measured **57.1%** (101/177) | ❌ **gate not met** at N=20 out-of-distribution |
| No success regression | 40% vs 40% (warm-up = measured; persona-limited) | ✅ no regression |
| Retrieval SLIs hold underneath | speculative hit **96.7%**, live-fallback **3.3%**, rerank p50 220 / p95 505 ms | ✅ |

**Plain reading:** with varied, unscripted callers and a cold tenant, a majority
of turns (57%) were answered verbatim from pre-drafted replies — but not the 70%
we committed to before claiming the feature. Per the protocol set in the kickoff
doc, the **feature ships enabled** (a miss costs nothing — the turn falls through
to live generation) and the **70% claim is NOT made**.

## What the data says about closing the gap

- **The rate climbs with history**: 44% with zero precedents → 57% within 20
  sessions, still rising at the end of the run (last five sessions: 55–60%).
  The predictor's fuel accumulates per tenant; N=20 is early on the curve.
- **Draft efficiency is the lever**: 221 drafts → 57 unique drafts served (26%),
  serving 101 turns. Most misses were state-mismatch (the draft existed for the
  right action but a different predicted state). Better next-state prediction —
  more history, mood conditioning — is the highest-value fix, ahead of drafting
  more combos per turn.
- **In-distribution ceiling is high**: the scripted smoke test (repeat callers,
  stable flows) hit 3/4 turns — call centers with recurring call shapes will sit
  between these numbers.

## Context numbers (measured window)

Mean turns/session 9.8 · pick rate 89.8% of turns · converse wall-clock p50
1.77 s / p95 3.43 s (includes classify + respond; instruction-hit turns skip the
respond call entirely) · outcomes 10 success / 5 failure / 10 abandoned (12-turn
cap dominates "abandoned" — persona conversations often still healthy at cap).

## Reproduce

```bash
sopilot-bench --api-key <key> --project main --sop-id <id> \
  --mode converse --sessions 20 --warmup 5 --out results.jsonl
```

Raw sessions: `bench/instruction_n20.jsonl`. Note: the `turns.instruction_hit`
column landed after this run; historical dashboard values for this window are a
partial backfill (first-serve turns only, 57/101) — live data from now on is
turn-accurate.

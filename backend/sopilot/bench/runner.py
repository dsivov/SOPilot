"""Bench session runner — drives the HTTP API end to end (online lane + live
supervisor), mirroring the POC's autopilot harness.

Per turn: user-sim speaks → proposer classifies state + picks an SOP-legal
action → POST plan-turn → responder plays the agent from the returned prompt.
The session ends when the classified state hits a success/failure marker or the
turn cap. Metrics come from plan-turn responses; warm-up sessions build the
precedent history and are reported separately from measured sessions.

Usage:
    sopilot-bench --api-key sop_... --project bench --sop-id <id> \
        --sessions 20 --warmup 3 --out bench_results.jsonl
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import time
import urllib.request

from ..schemas import TaskDefinition
from ..sop_graph import SOPGraph
from .sim import propose, respond_as_agent, sample_cohort_and_mood, simulate_user_turn

MAX_TURNS = 12

PERSONAS = [
    "Busy working parent; efficient, slightly impatient, wants the earliest possible slot.",
    "Elderly caller; polite but easily confused, asks the agent to repeat details.",
    "Price-sensitive; asks about costs and missed-appointment fees before committing.",
    "Wants to RESCHEDULE an existing appointment, not book a new one.",
    "Mentions symptoms got noticeably worse in the last two days (urgent path).",
    "Chatty and friendly; drifts off-topic once before getting to the point.",
    "Skeptical; had a bad experience last time and needs reassurance before booking.",
    "Calling on behalf of their mother (proxy caller).",
    "Straightforward booking; cooperative and quick to confirm.",
    "Undecided; hesitates about committing to a date and asks about the waiting list.",
]


def _call(base: str, method: str, path: str, body: dict | None, headers: dict) -> dict:
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Content-Type": "application/json", **headers},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read() or b"{}")


async def run_converse_session(
    base: str, headers: dict, sop_id: str, task_def: TaskDefinition, persona: str
) -> dict:
    """Drive the REAL runtime (/converse): the bench only simulates the caller.
    Instruction hits, classification, and replies all come from the server."""
    sess = _call(base, "POST", "/sessions", {"sop_id": sop_id}, headers)["session_id"]
    history: list[dict] = []
    turns: list[dict] = []
    outcome = "abandoned"
    for _ in range(MAX_TURNS):
        user_message = await simulate_user_turn(task_def, "", "", history, persona=persona)
        history.append({"role": "user_sim", "text": user_message})
        t0 = time.perf_counter()
        r = _call(base, "POST", f"/sessions/{sess}/converse", {"user_message": user_message}, headers)
        wall_ms = int((time.perf_counter() - t0) * 1000)
        history.append({"role": "agent", "text": r["reply"]})
        turns.append(
            {
                "turn_index": r["turn"]["turn_index"],
                "action": r["turn"]["chosen_action"],
                "state": r["classification"]["state"],
                "picks": len(r["turn"].get("picks", [])),
                "instruction_hit": bool(r["turn"].get("instruction_hit")),
                "rerank_ms": r["turn"].get("rerank_ms", 0),
                "plan_ms": wall_ms,
                "server_ms": r.get("total_ms", 0),
                "consume_stats": r["turn"].get("consume_stats", {}),
            }
        )
        if r.get("terminal"):
            outcome = r["terminal"]
            break
        await asyncio.sleep(2.0)  # inter-turn think-time: lets the supervisor land its work
    _call(base, "POST", f"/sessions/{sess}/outcome", {"outcome": outcome}, headers)
    _call(base, "POST", f"/sessions/{sess}/end", {}, headers)
    consumed = sum(t["consume_stats"].get("consumed", 0) for t in turns)
    live = sum(t["consume_stats"].get("live", 0) for t in turns)
    eligible = [t for t in turns if t["turn_index"] >= 1]
    return {
        "session_id": sess,
        "persona": persona[:60],
        "outcome": outcome,
        "turns": len(turns),
        "consumed": consumed,
        "live": live,
        "instruction_hits": sum(1 for t in turns if t["instruction_hit"]),
        "eligible_turns": len(eligible),
        "latency_hidden_ms": sum(t["consume_stats"].get("latency_hidden_ms", 0) for t in turns),
        "live_latency_ms": sum(t["consume_stats"].get("live_latency_ms", 0) for t in turns),
        "turn_details": turns,
    }


async def run_session(
    base: str,
    headers: dict,
    sop_id: str,
    task_def: TaskDefinition,
    rng: random.Random,
    proposer_model: str = "",
) -> dict:
    graph = SOPGraph(task_def)
    cohort, mood = sample_cohort_and_mood(task_def, rng)
    cp = task_def.conversation_profile
    success_markers = set(cp.success_markers)
    failure_markers = set(cp.failure_markers)

    sess = _call(base, "POST", "/sessions", {"sop_id": sop_id}, headers)["session_id"]
    history: list[dict] = []
    visited: set[str] = set()
    turns: list[dict] = []
    outcome = "abandoned"
    prev_agent_reply: str | None = None

    for turn_index in range(MAX_TURNS):
        user_message = await simulate_user_turn(task_def, cohort, mood, history)
        history.append({"role": "user_sim", "text": user_message})

        allowed = graph.allowed_actions(visited)
        proposal = await propose(
            task_def, cohort, history, user_message, allowed, model=proposer_model
        )
        state = proposal["state"]

        t0 = time.perf_counter()
        plan = _call(
            base,
            "POST",
            f"/sessions/{sess}/plan-turn",
            {
                "user_message": user_message,
                "cohort": cohort,
                "mood": proposal["mood"] or mood,
                "state": state,
                "action": proposal["action"],
                "prev_assistant_message": prev_agent_reply,
            },
            headers,
        )
        plan_ms = int((time.perf_counter() - t0) * 1000)

        visited.add(plan["chosen_action"])
        if state:
            visited.add(state)

        agent_reply = await respond_as_agent(plan.get("prompt_text") or "", history, user_message)
        history.append({"role": "agent", "text": agent_reply})
        prev_agent_reply = agent_reply

        turns.append(
            {
                "turn_index": plan["turn_index"],
                "action": plan["chosen_action"],
                "state": state,
                "picks": len(plan.get("picks", [])),
                "instruction_hit": plan.get("instruction_hit", False),
                "rerank_ms": plan.get("rerank_ms", 0),
                "plan_ms": plan_ms,
                "consume_stats": plan.get("consume_stats", {}),
            }
        )
        if state in success_markers:
            outcome = "success"
            break
        if state in failure_markers:
            outcome = "failure"
            break

    _call(base, "POST", f"/sessions/{sess}/outcome", {"outcome": outcome}, headers)
    _call(base, "POST", f"/sessions/{sess}/end", {}, headers)

    consumed = sum(t["consume_stats"].get("consumed", 0) for t in turns)
    live = sum(t["consume_stats"].get("live", 0) for t in turns)
    return {
        "session_id": sess,
        "cohort": cohort,
        "mood": mood,
        "outcome": outcome,
        "turns": len(turns),
        "consumed": consumed,
        "live": live,
        "latency_hidden_ms": sum(t["consume_stats"].get("latency_hidden_ms", 0) for t in turns),
        "live_latency_ms": sum(t["consume_stats"].get("live_latency_ms", 0) for t in turns),
        "turn_details": turns,
    }


def summarize(sessions: list[dict], label: str) -> dict:
    if not sessions:
        return {"label": label, "sessions": 0}
    all_turns = [t for s in sessions for t in s["turn_details"]]
    data_turns = [t for t in all_turns if t["consume_stats"].get("consumed", 0) + t["consume_stats"].get("live", 0) > 0]
    consumed = sum(s["consumed"] for s in sessions)
    live = sum(s["live"] for s in sessions)
    rerank = sorted(t["rerank_ms"] for t in all_turns)
    plan = sorted(t["plan_ms"] for t in all_turns)
    pick_turns = [t for t in all_turns if t["picks"] > 0]

    def pct(xs: list, q: float) -> float:
        return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else 0.0

    instr_hits = sum(s.get("instruction_hits", 0) for s in sessions)
    eligible = sum(s.get("eligible_turns", 0) for s in sessions)
    return {
        "label": label,
        "sessions": len(sessions),
        "success_rate": round(sum(1 for s in sessions if s["outcome"] == "success") / len(sessions), 3),
        "instruction_hit_rate": round(instr_hits / eligible, 3) if eligible else None,
        "instruction_hits": instr_hits,
        "eligible_turns": eligible,
        "mean_turns": round(statistics.mean(s["turns"] for s in sessions), 1),
        "speculative_hit_rate": round(consumed / (consumed + live), 3) if consumed + live else None,
        "live_fallback_rate": round(live / (consumed + live), 3) if consumed + live else None,
        "data_turns": len(data_turns),
        "pick_rate_all_turns": round(len(pick_turns) / len(all_turns), 3) if all_turns else None,
        "rerank_ms_p50": pct(rerank, 0.50),
        "rerank_ms_p95": pct(rerank, 0.95),
        "plan_turn_ms_p50": pct(plan, 0.50),
        "plan_turn_ms_p95": pct(plan, 0.95),
        "latency_hidden_ms_mean_per_session": int(statistics.mean(s["latency_hidden_ms"] for s in sessions)),
    }


async def amain(args: argparse.Namespace) -> None:
    headers = {"Authorization": f"Bearer {args.api_key}", "X-Project": args.project}
    sop = _call(args.base, "GET", f"/sops/{args.sop_id}", None, headers)
    task_def = TaskDefinition.model_validate(sop["definition"])
    rng = random.Random(args.seed)

    results: list[dict] = []
    total = args.warmup + args.sessions
    for i in range(total):
        phase = "warmup" if i < args.warmup else "measured"
        t0 = time.perf_counter()
        if args.mode == "converse":
            persona = PERSONAS[i % len(PERSONAS)]
            s = await run_converse_session(args.base, headers, args.sop_id, task_def, persona)
        else:
            s = await run_session(
                args.base, headers, args.sop_id, task_def, rng, proposer_model=args.proposer_model
            )
        s["phase"] = phase
        results.append(s)
        print(
            f"[{i + 1}/{total}] {phase} outcome={s['outcome']} turns={s['turns']} "
            f"consumed={s['consumed']} live={s['live']} instr={s.get('instruction_hits', 0)}/{s.get('eligible_turns', 0)} "
            f"hidden={s['latency_hidden_ms']}ms ({time.perf_counter() - t0:.1f}s)"
        )

    with open(args.out, "w") as f:
        for s in results:
            f.write(json.dumps(s) + "\n")

    warm = summarize([s for s in results if s["phase"] == "warmup"], "warmup")
    measured = summarize([s for s in results if s["phase"] == "measured"], "measured")
    print("\n=== SUMMARY ===")
    for block in (warm, measured):
        print(json.dumps(block, indent=2))
    print(f"\nper-session details: {args.out}")


def main() -> None:
    p = argparse.ArgumentParser(description="SOPilot bench runner")
    p.add_argument("--base", default="http://127.0.0.1:8100")
    p.add_argument("--api-key", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--sop-id", required=True)
    p.add_argument("--sessions", type=int, default=20)
    p.add_argument("--warmup", type=int, default=3)
    p.add_argument("--seed", type=int, default=17)
    p.add_argument("--proposer-model", default="", help="stronger model for state/action classification")
    p.add_argument("--mode", choices=["converse", "plan"], default="converse",
                   help="converse = drive the real runtime; plan = legacy bench-side roles")
    p.add_argument("--out", default="bench_results.jsonl")
    args = p.parse_args()
    asyncio.run(amain(args))


if __name__ == "__main__":
    main()

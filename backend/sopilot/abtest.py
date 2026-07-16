"""Autopilot A/B: drive two SOP versions through the REAL runtime with the
customer simulator, judge every turn, aggregate per turn index.

The runner talks to our own HTTP API (same pattern as the bench harness) so
each arm exercises the full production path — classification, pool, prompts,
instruction prefetch — not a shortcut. The caller's bearer key is reused for
the self-calls and never stored.

Per-turn metrics (Feature B of docs/NEW_FEATURES.md):
  - accuracy      LLM judge: did the agent's reply follow the SOP stage it was
                  on (mandated wording, right action, no skipped verification)? 0..1
  - response_ms   server total_ms for the turn (classify + plan + respond)
  - satisfaction  LLM judge: how satisfied is THIS persona right now? 1..5
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.request
from typing import Any

from .bench.llm import chat_json
from .bench.runner import PERSONAS
from .bench.sim import simulate_user_turn
from .db import get_sessionmaker
from .models import ABTest, utcnow
from .schemas import TaskDefinition

log = logging.getLogger("sopilot.abtest")

SELF_BASE = "http://127.0.0.1:8100"


def _call_sync(base: str, method: str, path: str, body: dict | None, headers: dict) -> dict:
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Content-Type": "application/json", **headers},
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read() or b"{}")


async def _call(base: str, method: str, path: str, body: dict | None, headers: dict) -> dict:
    """Self-HTTP MUST leave the event loop (to_thread) — a blocking call to our
    own server from inside its loop deadlocks until timeout."""
    return await asyncio.to_thread(_call_sync, base, method, path, body, headers)


async def _judge_turn(
    task_def: TaskDefinition, persona: str, history: list[dict], user_message: str, reply: str, action: str
) -> dict:
    """One cheap judge call scoring both metrics for a single turn."""
    stage = next((a for a in task_def.agent_actions if a.name == action), None)
    stage_desc = (
        f"Stage the agent was executing: {action} — {stage.description}\n"
        + (f"Mandated wording: {'; '.join(stage.must_say)}\n" if stage and stage.must_say else "")
        if stage
        else ""
    )
    transcript = "\n".join(f"{h['role']}: {h['text']}" for h in history[-6:])
    out = await chat_json(
        "You evaluate ONE turn of a procedure-following phone agent. Return JSON: "
        '{"adherence": <0.0-1.0 — did the reply execute the stage it was on: right intent, '
        "mandated wording carried, no skipped verification, no invented policy>, "
        '"satisfaction": <1-5 — how satisfied is this specific customer with the reply, given their persona>, '
        '"note": "<max 12 words>"}',
        [
            {
                "role": "user",
                "content": (
                    f"Procedure: {task_def.description}\n{stage_desc}"
                    f"Customer persona: {persona}\n\nRecent transcript:\n{transcript}\n\n"
                    f"Customer said: {user_message}\nAgent replied: {reply}"
                ),
            }
        ],
    )
    try:
        adherence = max(0.0, min(1.0, float(out.get("adherence", 0.5))))
    except (TypeError, ValueError):
        adherence = 0.5
    try:
        satisfaction = max(1.0, min(5.0, float(out.get("satisfaction", 3))))
    except (TypeError, ValueError):
        satisfaction = 3.0
    return {"adherence": adherence, "satisfaction": satisfaction}


async def _run_arm_session(
    headers: dict, sop_id: str, sop_version: int, task_def: TaskDefinition, persona: str, max_turns: int
) -> dict:
    sess = (
        await _call(
            SELF_BASE,
            "POST",
            "/sessions",
            {"sop_id": sop_id, "sop_version": sop_version, "channel": "bench"},
            headers,
        )
    )["session_id"]
    history: list[dict] = []
    turns: list[dict] = []
    outcome = "abandoned"
    for _ in range(max_turns):
        user_message = await simulate_user_turn(task_def, "", "", history, persona=persona)
        history.append({"role": "user_sim", "text": user_message})
        r = await _call(SELF_BASE, "POST", f"/sessions/{sess}/converse", {"user_message": user_message}, headers)
        reply = r["reply"]
        history.append({"role": "agent", "text": reply})
        judge = await _judge_turn(
            task_def, persona, history, user_message, reply, r["classification"].get("action") or ""
        )
        turns.append(
            {
                "turn_index": r["turn"]["turn_index"],
                "response_ms": r.get("total_ms", 0),
                "accuracy": judge["adherence"],
                "satisfaction": judge["satisfaction"],
                "instruction_hit": bool(r["turn"].get("instruction_hit")),
            }
        )
        if r.get("terminal"):
            outcome = r["terminal"]
            break
        await asyncio.sleep(1.0)  # let the supervisor land prefetch work between turns
    await _call(SELF_BASE, "POST", f"/sessions/{sess}/outcome", {"outcome": outcome}, headers)
    await _call(SELF_BASE, "POST", f"/sessions/{sess}/end", None, headers)
    return {"session_id": sess, "persona": persona[:60], "outcome": outcome, "turns": turns}


def _aggregate(sessions: list[dict]) -> dict:
    by_turn: dict[int, dict[str, list[float]]] = {}
    for s in sessions:
        for t in s["turns"]:
            b = by_turn.setdefault(t["turn_index"], {"accuracy": [], "response_ms": [], "satisfaction": []})
            b["accuracy"].append(t["accuracy"])
            b["response_ms"].append(t["response_ms"])
            b["satisfaction"].append(t["satisfaction"])
    mean = lambda xs: round(sum(xs) / len(xs), 3) if xs else None  # noqa: E731
    per_turn = [
        {
            "turn_index": i,
            "n": len(v["accuracy"]),
            "accuracy": mean(v["accuracy"]),
            "response_ms": mean(v["response_ms"]),
            "satisfaction": mean(v["satisfaction"]),
        }
        for i, v in sorted(by_turn.items())
    ]
    outcomes: dict[str, int] = {}
    for s in sessions:
        outcomes[s["outcome"]] = outcomes.get(s["outcome"], 0) + 1
    all_t = [t for s in sessions for t in s["turns"]]
    return {
        "per_turn": per_turn,
        "sessions": len(sessions),
        "outcomes": outcomes,
        "success_rate": round(outcomes.get("success", 0) / len(sessions), 3) if sessions else None,
        "avg_turns": round(sum(len(s["turns"]) for s in sessions) / len(sessions), 1) if sessions else None,
        "overall": {
            "accuracy": mean([t["accuracy"] for t in all_t]),
            "response_ms": mean([t["response_ms"] for t in all_t]),
            "satisfaction": mean([t["satisfaction"] for t in all_t]),
        },
        "session_details": sessions,
    }


async def run_abtest(abtest_id: str, bearer_key: str, project_slug: str) -> None:
    """Background task body. Owns its DB sessions; persists progress as it goes."""
    headers = {"Authorization": f"Bearer {bearer_key}", "X-Project": project_slug}
    maker = get_sessionmaker()
    async with maker() as db:
        test = await db.get(ABTest, abtest_id)
        if test is None:
            return
        sop_id, n, max_turns = test.sop_id, test.n_sessions, test.max_turns
        arms = {"A": test.arm_a_version, "B": test.arm_b_version}

    try:
        defs: dict[str, TaskDefinition] = {}
        for arm, ver in arms.items():
            sop = await _call(SELF_BASE, "GET", f"/sops/{sop_id}?version={ver}", None, headers)
            defs[arm] = TaskDefinition.model_validate(sop["definition"])

        total = n * 2
        done = 0
        results: dict[str, list[dict]] = {"A": [], "B": []}
        # alternate arms so drift (predictor warm-up, time of day) hits both equally
        for i in range(n):
            persona = PERSONAS[i % len(PERSONAS)]
            for arm in ("A", "B"):
                results[arm].append(
                    await _run_arm_session(headers, sop_id, arms[arm], defs[arm], persona, max_turns)
                )
                done += 1
                async with maker() as db:
                    test = await db.get(ABTest, abtest_id)
                    test.progress = {"completed": done, "total": total}
                    await db.commit()

        summary = {
            "arm_a": {"sop_version": arms["A"], **_aggregate(results["A"])},
            "arm_b": {"sop_version": arms["B"], **_aggregate(results["B"])},
        }
        async with maker() as db:
            test = await db.get(ABTest, abtest_id)
            test.results = summary
            test.status = "done"
            test.finished_at = utcnow()
            await db.commit()
    except Exception as exc:  # noqa: BLE001 — a failed run must land in the row, not a log only
        log.exception("abtest %s failed", abtest_id)
        async with maker() as db:
            test = await db.get(ABTest, abtest_id)
            if test is not None:
                test.status = "failed"
                test.error = f"{type(exc).__name__}: {exc}"[:500]
                test.finished_at = utcnow()
                await db.commit()

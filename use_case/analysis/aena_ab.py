#!/usr/bin/env python3
"""AENA two-arm A/B — the proven methodology (SOPBench design, local corpus).

Arm A_prompt  plain LLM (gpt-4o), everything in the prompt: all three mined
              SOP documents + the complete fact base. Internalized compliance.
Arm A_rag     plain LLM (gpt-4o) + per-turn RAG over the SAME fact base (top-3
              by embedding) — knowledge access matches SOPilot's; no SOP layer.
Arm B         SOPilot runtime end to end (intake router → SOP tracking →
              connectors → prompts) via /converse.

Both arms face the SAME simulated traveller: each scenario derives from a
held-out REAL dialogue — the traveller opens with the real first utterance and
pursues the real need; ground truth = what the human desk agent actually
provided in that conversation (from the replay judge's extraction). A judge
scores each finished conversation: goal coverage vs ground truth, specifics
(did the agent give the concrete location/number when one exists), and
traveller satisfaction.

Usage: backend/.venv/bin/python aena_ab.py [n_per_theme]
Emits aena_ab_results.json (local; reports quote aggregates only).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time as _time
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
for line in (HERE.parent.parent / "backend/.env").read_text().splitlines():
    if line.strip() and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

from openai import AsyncOpenAI  # noqa: E402

sys.path.insert(0, str(HERE))
from mine_sop import THEMES  # noqa: E402
from replay import SOP_IDS, heldout  # noqa: E402

client = AsyncOpenAI()
BASE = "http://127.0.0.1:8100"
KEY = "sop_19b6179b1c913671df2251e9ac2eb70d9b817c91"
N_PER_THEME = int(sys.argv[1]) if len(sys.argv) > 1 else 12
MAX_TURNS = 4
AGENT_MODEL = "gpt-4o-mini"  # prod-realistic responder class (matches SOPILOT_RESPOND_MODEL)
SIM_MODEL = "gpt-4o-mini"  # traveller simulator — identical for both arms


def _call(method: str, path: str, body: dict | None) -> dict:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode() if body is not None else None, method=method,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}", "X-Project": "malaga"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=180).read())


async def call(method: str, path: str, body: dict | None = None) -> dict:
    return await asyncio.to_thread(_call, method, path, body)


def load_scenarios() -> list[dict]:
    """Held-out dialogues + the replay judge's ground-truth extraction."""
    truth: dict[str, str] = {}
    for fname in ("replay_results_v1.json", "replay_results.json"):
        f = HERE / fname
        if f.exists():
            for r in json.load(f.open())["results"]:
                if r.get("human_gave"):
                    truth[r["id"]] = r["human_gave"]
    scenarios = []
    for theme, cfg in THEMES.items():
        for d in heldout(cfg, N_PER_THEME):
            opening = next((t["t"] for t in d["turns"] if t["s"] == "Client" and len(t["t"].strip()) >= 15), None)
            if not opening:
                continue
            scenarios.append({
                "id": d["id"], "theme": theme, "opening": opening.strip()[:300],
                "truth": truth.get(d["id"], ""), "lang": d.get("lang", "es"),
            })
    return scenarios


def knowledge_pack() -> str:
    sops = "\n\n".join((HERE / "mined" / f"sop_{t}.txt").read_text() for t in THEMES)
    facts = "\n".join("- " + json.loads(ln)["text"] for ln in (HERE / "facts_full.jsonl").open())
    return f"STANDARD OPERATING PROCEDURES:\n{sops}\n\nAIRPORT FACT BASE:\n{facts}"


PACK = knowledge_pack()

SIM_SYS = (
    "You are a traveller at Málaga airport talking to the information desk. Stay fully in character; reply with "
    "ONLY your next utterance (1-2 short spoken sentences, keep the language you started in).\n"
    "Your need: {need}\nYou opened with: \"{opening}\"\n"
    "Pursue your need naturally. If the agent has clearly given you the information (or clearly routed you to the "
    "right place), say thanks and end with the single token DONE at the end of your utterance. If the agent is "
    "unhelpful twice in a row, give up politely and end with DONE."
)

JUDGE_SYS = (
    "You judge ONE airport information-desk conversation between a traveller and an AI agent, against ground truth "
    "extracted from how a REAL human desk agent handled the same need.\n"
    'Return JSON {"coverage": "covered"|"partial"|"missed" — did the agent provide the essential '
    "information/routing in the ground truth (or a demonstrably valid equivalent); "
    '"specifics": true|false — when the ground truth contains a concrete location/number/name, did the agent give '
    'a concrete one (not "check the screens"); "satisfaction": 1-5 — from the traveller\'s perspective; '
    '"note": "<max 12 words>"}'
)


async def sim_turn(scenario: dict, history: list[dict]) -> str:
    msgs = [{"role": "assistant" if h["role"] == "traveller" else "user", "content": h["text"]} for h in history]
    if not msgs:
        return scenario["opening"]
    res = await client.chat.completions.create(
        model=SIM_MODEL, temperature=0.3, max_tokens=90,
        messages=[{"role": "system", "content": SIM_SYS.format(
            need=scenario["truth"] or "the need implied by your opening", opening=scenario["opening"])}, *msgs],
    )
    return (res.choices[0].message.content or "").strip()


CG_URL = "http://10.0.0.80:9621/query"
CG_BODY = {"mode": "mix", "only_need_context": True, "chunk_top_k": 5, "max_total_tokens": 3000}


def _cg_query_sync(query: str) -> str:
    req = urllib.request.Request(
        CG_URL, data=json.dumps({**CG_BODY, "query": query}).encode(), method="POST",
        headers={"Content-Type": "application/json", "LIGHTRAG-WORKSPACE": "aena"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=60).read()).get("response", "")[:3000]


async def _cg_retrieve(query: str) -> str:
    """Per-turn retrieval from the PRODUCTION Context Graph server — the same
    system and parameters the SOPilot connector uses; A_rag pays its latency
    on-path every turn, SOPilot prefetches it in the background."""
    return await asyncio.to_thread(_cg_query_sync, query)


BASE_SYS = ("You are the Málaga Airport information desk agent. Answer concretely and briefly "
            "(spoken, 1-3 sentences), in the traveller's language.")


async def arm_a_reply(history: list[dict], variant: str) -> str:
    msgs = [{"role": "user" if h["role"] == "traveller" else "assistant", "content": h["text"]} for h in history]
    if variant == "A_prompt":
        system = BASE_SYS + " Follow the procedures and use the fact base.\n\n" + PACK
    else:  # A_rag — same retrieval access as SOPilot, no SOP layer
        last = next(h["text"] for h in reversed(history) if h["role"] == "traveller")
        system = BASE_SYS + "\n\nRETRIEVED CONTEXT (production knowledge server, this turn):\n" + await _cg_retrieve(last)
    res = await client.chat.completions.create(
        model=AGENT_MODEL, temperature=0.3, max_tokens=200,
        messages=[{"role": "system", "content": system}, *msgs],
    )
    return (res.choices[0].message.content or "").strip()


async def run_conversation(scenario: dict, arm: str) -> dict:
    history: list[dict] = []
    latencies: list[int] = []
    terminal_seen = None
    sess = None
    if arm == "B":
        sess = (await call("POST", "/sessions", {"channel": "bench"}))["session_id"]  # intake — router decides
    for _ in range(MAX_TURNS):
        utt = await sim_turn(scenario, history)
        done = utt.rstrip().endswith("DONE")
        utt = utt.rstrip().removesuffix("DONE").strip()
        if utt:
            history.append({"role": "traveller", "text": utt})
            t0 = _time.perf_counter()
            if arm.startswith("A"):
                reply = await arm_a_reply(history, arm)
            else:
                r = await call("POST", f"/sessions/{sess}/converse", {"user_message": utt})
                reply = r["reply"]
                if r.get("terminal"):
                    terminal_seen = r["terminal"]
            latencies.append(int((_time.perf_counter() - t0) * 1000))
            history.append({"role": "agent", "text": reply})
        if done:
            break
        await asyncio.sleep(0.8)
    if sess:
        await call("POST", f"/sessions/{sess}/outcome", {"outcome": terminal_seen or "abandoned"})
        await call("POST", f"/sessions/{sess}/end")

    transcript = "\n".join(f"{h['role']}: {h['text']}" for h in history)
    res = await client.chat.completions.create(
        model="gpt-4o", temperature=0, max_tokens=300, response_format={"type": "json_object"},
        messages=[{"role": "system", "content": JUDGE_SYS},
                  {"role": "user", "content": f"GROUND TRUTH (human desk agent): {scenario['truth'] or '(unknown — judge general helpfulness)'}\n\nCONVERSATION:\n{transcript}"}],
    )
    try:
        verdict = json.loads(res.choices[0].message.content)
    except json.JSONDecodeError:
        verdict = {"coverage": "missed", "specifics": False, "satisfaction": 1, "note": "judge parse error"}
    return {"id": scenario["id"], "theme": scenario["theme"], "arm": arm,
            "turns": sum(1 for h in history if h["role"] == "traveller"),
            "latencies_ms": latencies, **verdict}


async def main() -> None:
    scenarios = load_scenarios()
    print(f"{len(scenarios)} scenarios ({N_PER_THEME}/theme)")
    sem = asyncio.Semaphore(3)

    async def guarded(s: dict, arm: str) -> dict:
        async with sem:
            try:
                return await run_conversation(s, arm)
            except Exception as e:  # noqa: BLE001
                return {"id": s["id"], "theme": s["theme"], "arm": arm, "coverage": "error",
                        "specifics": False, "satisfaction": 0, "note": str(e)[:80], "turns": 0}

    results = await asyncio.gather(*(guarded(s, arm) for s in scenarios for arm in ("A_prompt", "A_rag", "B")))
    agg: dict[str, dict] = {}
    for r in results:
        a = agg.setdefault(r["arm"], {"covered": 0, "partial": 0, "missed": 0, "error": 0,
                                       "specifics": 0, "sat": [], "turns": []})
        a[r["coverage"]] = a.get(r["coverage"], 0) + 1
        a["specifics"] += bool(r.get("specifics"))
        if r.get("satisfaction"):
            a["sat"].append(r["satisfaction"])
        a["turns"].append(r.get("turns", 0))
    lat: dict[str, list[int]] = {}
    for r in results:
        lat.setdefault(r["arm"], []).extend(r.get("latencies_ms") or [])
    for arm in ("A_prompt", "A_rag", "B"):
        a = agg[arm]
        n = a["covered"] + a["partial"] + a["missed"]
        xs = sorted(lat.get(arm) or [0])
        q = lambda f: xs[min(len(xs) - 1, int(f * len(xs)))]
        print(f"ARM {arm}: covered {a['covered']}/{n} ({100*a['covered']/n:.0f}%)  partial {a['partial']}  "
              f"missed {a['missed']}  specifics {a['specifics']}/{n}  "
              f"satisfaction {sum(a['sat'])/len(a['sat']):.2f}  errors {a['error']}  "
              f"| reply-latency p50 {q(0.5)}ms p95 {q(0.95)}ms (n={len(xs)})")
    (HERE / "aena_ab_results.json").write_text(json.dumps(
        {"aggregate": {k: {kk: vv for kk, vv in v.items() if kk not in ("sat", "turns")} for k, v in agg.items()},
         "satisfaction": {k: round(sum(v["sat"]) / max(1, len(v["sat"])), 2) for k, v in agg.items()},
         "latency_ms": {k: {"p50": sorted(v)[len(v)//2], "p95": sorted(v)[int(0.95*len(v))-1], "mean": sum(v)//len(v)} for k, v in lat.items() if v},
         "results": results}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    asyncio.run(main())

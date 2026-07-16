#!/usr/bin/env python3
"""Replay evaluation: held-out REAL traveller questions through the aena POC.

For each held-out dialogue (never seen by mining): replay the client's turns,
in order, against a fresh session on the theme's SOP; then a judge compares
the POC agent's answers with what the HUMAN agent actually provided in that
conversation. Coverage verdict per dialogue: covered / partial / missed.

Aggregates land in replay_results.json (local; the report quotes numbers only).

Usage: backend/.venv/bin/python replay.py [n_per_theme]
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
for line in (HERE.parent.parent / "backend/.env").read_text().splitlines():
    if line.strip() and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

from openai import AsyncOpenAI  # noqa: E402

sys.path.insert(0, str(HERE))
from mine_sop import THEMES, pick_dialogues  # noqa: E402  (same selection → exclude = held-out)

BASE = "http://127.0.0.1:8100"
KEY = "sop_19b6179b1c913671df2251e9ac2eb70d9b817c91"
SOP_IDS = {
    "lost_luggage": "95daeb480e344545bbbfb7f4a62102bb",
    "flight_info": "58177bc1585840cbae27c076421e6ddc",
    "transport_wayfinding": "91f6097586424b2f913b6e7b5b0ac992",
}
N_PER_THEME = int(sys.argv[1]) if len(sys.argv) > 1 else 12
MAX_CLIENT_TURNS = 4
client = AsyncOpenAI()

JUDGE_SYS = (
    "You compare a human airport-desk agent's answers with an AI agent's answers to the SAME traveller questions "
    "(Spanish/English; the human transcript is noisy speech-to-text). Judge INFORMATION COVERAGE only: did the AI "
    "provide the essential information/routing the human provided (destination desk, location, procedure, key "
    "facts)? Politeness and asking reasonable clarifying questions are fine and do not count against coverage. "
    'Return JSON: {"verdict": "covered" | "partial" | "missed", '
    '"human_gave": "<the essential info in one sentence>", "ai_gave": "<one sentence>", "note": "<max 15 words>"}'
)


def _call(method: str, path: str, body: dict | None) -> dict:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body else None,
        method=method,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}", "X-Project": "malaga"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=180).read())


async def call(method: str, path: str, body: dict | None = None) -> dict:
    return await asyncio.to_thread(_call, method, path, body)


def heldout(theme_cfg: dict, n: int) -> list[dict]:
    used = {r["id"] for r in pick_dialogues(theme_cfg["labels"])}
    per = {d["id"]: d for d in json.load((HERE / "intents.json").open())["per_dialogue"]}
    rows = []
    for ln in (HERE / "cleaned.jsonl").open(encoding="utf-8"):
        r = json.loads(ln)
        lab = per.get(r["id"])
        if r["id"] in used or not lab or not lab["labels"] or lab["labels"][0] not in theme_cfg["labels"]:
            continue
        if not (4 <= r["stats"]["turns"] <= 16):
            continue
        rows.append(r)
    rows.sort(key=lambda r: r["id"])  # deterministic held-out sample
    return rows[:n]


async def replay_one(theme: str, dialogue: dict, sem: asyncio.Semaphore) -> dict:
    async with sem:
        client_turns = [t["t"] for t in dialogue["turns"] if t["s"] == "Client"][:MAX_CLIENT_TURNS]
        sess = (await call("POST", "/sessions", {"sop_id": SOP_IDS[theme], "channel": "bench"}))["session_id"]
        ai_replies = []
        terminal_seen = None
        for msg in client_turns:
            r = await call("POST", f"/sessions/{sess}/converse", {"user_message": msg})
            ai_replies.append(r["reply"])
            if r.get("terminal"):
                terminal_seen = r["terminal"]
                break
            await asyncio.sleep(1.0)
        await call("POST", f"/sessions/{sess}/outcome", {"outcome": terminal_seen or "abandoned"})
        await call("POST", f"/sessions/{sess}/end")

        human = "\n".join(f"{t['s']}: {t['t']}" for t in dialogue["turns"])
        ai = "\n".join(
            f"Client: {q}\nAI-Agent: {a}" for q, a in zip(client_turns, ai_replies)
        )
        res = await client.chat.completions.create(
            model="gpt-4o",
            temperature=0,
            max_tokens=400,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": JUDGE_SYS},
                {"role": "user", "content": f"ORIGINAL (human agent):\n{human}\n\nREPLAY (AI agent):\n{ai}"},
            ],
        )
        verdict = json.loads(res.choices[0].message.content)
        return {"theme": theme, "id": dialogue["id"], "turns_replayed": len(ai_replies), **verdict}


async def main() -> None:
    sem = asyncio.Semaphore(3)
    tasks = []
    for theme, cfg in THEMES.items():
        for d in heldout(cfg, N_PER_THEME):
            tasks.append(replay_one(theme, d, sem))
    results = await asyncio.gather(*tasks)
    agg: dict[str, dict[str, int]] = {}
    for r in results:
        agg.setdefault(r["theme"], {"covered": 0, "partial": 0, "missed": 0})[r["verdict"]] += 1
    (HERE / "replay_results.json").write_text(json.dumps({"aggregate": agg, "results": results}, ensure_ascii=False, indent=1))
    total = {"covered": 0, "partial": 0, "missed": 0}
    for theme, a in agg.items():
        n = sum(a.values())
        print(f"{theme:22} covered {a['covered']}/{n}  partial {a['partial']}  missed {a['missed']}")
        for k in total:
            total[k] += a[k]
    n = sum(total.values())
    print(f"{'TOTAL':22} covered {total['covered']}/{n} ({100*total['covered']/n:.0f}%)  partial {total['partial']} ({100*total['partial']/n:.0f}%)  missed {total['missed']}")


if __name__ == "__main__":
    asyncio.run(main())

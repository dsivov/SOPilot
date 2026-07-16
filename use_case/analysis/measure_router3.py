#!/usr/bin/env python3
"""Router measurement v3 — oracle decomposition.

gpt-4o (strong) routes the SAME sample's first-two-utterances, with an
explicit routable/not-routable-yet judgment. Separates three quantities:
  1. label noise: oracle vs whole-dialogue lexicon label agreement,
  2. router quality: gpt-4o-mini vs oracle agreement on routable openings,
  3. deferral share: openings whose theme has not appeared yet — the case
     intake mode handles by simply waiting one more turn.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
from collections import Counter
from pathlib import Path

HERE = Path(__file__).parent
for line in (HERE.parent.parent / "backend/.env").read_text().splitlines():
    if line.strip() and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

from openai import AsyncOpenAI  # noqa: E402

from measure_router2 import CLASS_OF, LLM_SYS, first_utts, llm_route  # noqa: E402

client = AsyncOpenAI()

ORACLE_SYS = (
    "You are an expert triage supervisor at the Málaga airport information desk, reading the OPENING of a "
    "conversation (first traveller utterances, noisy speech-to-text, Spanish/English).\n"
    "Procedures: sop1 = lost/delayed CHECKED LUGGAGE; sop2 = flight/check-in/boarding information; "
    "sop3 = ground transport/parking/where-is-X wayfinding; oos = out of scope for these three.\n"
    'Return JSON {"routable": true|false, "route": "sop1|sop2|sop3|oos", '
    '"reason": "<max 10 words>"}. routable=false means the opening does not yet reveal what the traveller '
    "needs (pure greeting, unintelligible, cut off) — a human would wait for the next sentence."
)


async def oracle(text: str, sem: asyncio.Semaphore) -> dict:
    async with sem:
        res = await client.chat.completions.create(
            model="gpt-4o", temperature=0, max_tokens=80,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": ORACLE_SYS}, {"role": "user", "content": text}],
        )
        try:
            return json.loads(res.choices[0].message.content)
        except json.JSONDecodeError:
            return {"routable": False, "route": "oos"}


async def main() -> None:
    per = {d["id"]: d for d in json.load((HERE / "intents.json").open())["per_dialogue"]}
    rows = []
    for ln in (HERE / "cleaned.jsonl").open(encoding="utf-8"):
        r = json.loads(ln)
        utt = first_utts(r)
        if not utt:
            continue
        lab = per.get(r["id"])
        primary = lab["labels"][0] if lab and lab["labels"] else None
        rows.append({"id": r["id"], "utt": utt, "cls": CLASS_OF.get(primary, "oos")})

    rng = random.Random(7)
    sample: list[dict] = []
    for cls in ("sop1", "sop2", "sop3", "oos"):
        pool = [r for r in rows if r["cls"] == cls]
        sample += rng.sample(pool, min(150, len(pool)))

    sem = asyncio.Semaphore(16)
    oracles, cheaps = await asyncio.gather(
        asyncio.gather(*(oracle(r["utt"], sem) for r in sample)),
        asyncio.gather(*(llm_route(r["utt"], sem) for r in sample)),
    )

    n = len(sample)
    deferral = sum(1 for o in oracles if not o.get("routable"))
    routable = [(r, o, c) for r, o, c in zip(sample, oracles, cheaps) if o.get("routable")]
    ora_vs_label = sum(1 for r, o, _ in routable if o["route"] == r["cls"]) / len(routable)
    cheap_vs_ora = sum(1 for _, o, c in routable if c == o["route"]) / len(routable)
    in_scope_routable = [(r, o, c) for r, o, c in routable if o["route"] != "oos"]
    cheap_vs_ora_committed = (
        sum(1 for _, o, c in in_scope_routable if c == o["route"]) / len(in_scope_routable)
    )
    conf = Counter((o["route"], r["cls"]) for r, o, _ in routable)

    print(f"sample {n} · oracle says NOT-ROUTABLE-YET: {deferral} ({deferral/n:.1%}) — intake waits a turn")
    print(f"oracle vs weak whole-dialogue label (routable only): {ora_vs_label:.1%}  <- label-noise + late-theme bound")
    print(f"cheap router vs oracle (all routable): {cheap_vs_ora:.1%}")
    print(f"cheap router vs oracle (oracle committed to a SOP): {cheap_vs_ora_committed:.1%}")
    print("oracle-route vs weak-label confusion:", dict(conf))
    (HERE / "router_measurement_v3.json").write_text(json.dumps(
        {"n": n, "deferral_rate": round(deferral / n, 4),
         "oracle_vs_weak_label": round(ora_vs_label, 4),
         "cheap_vs_oracle": round(cheap_vs_ora, 4),
         "cheap_vs_oracle_committed": round(cheap_vs_ora_committed, 4),
         "confusion_oracle_vs_label": {f"{a}->{b}": v for (a, b), v in conf.items()}}, indent=1))


if __name__ == "__main__":
    asyncio.run(main())

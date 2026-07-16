#!/usr/bin/env python3
"""D-11 pre-build measurement: can embedding kNN over mined exemplar
utterances route the first traveller utterance to the right SOP?

Setup
  classes   sop1=lost_luggage · sop2=flight_info · sop3=transport_wayfinding
            (transport_parking + wayfinding) · OOS = every other/unmatched label
  exemplars 20% of in-scope dialogues (deterministic id-hash split) donate
            their first substantive client utterance to their class's pool
  eval      the other 80% (in-scope: accuracy; OOS: rejection at threshold)
  score     max cosine vs each class pool (kNN max), route = argmax if ≥ τ

Ground truth is the keyword-lexicon label — imperfect; treat results as a
floor. Emits router_measurement.json + prints the operating table.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections import Counter
from pathlib import Path

HERE = Path(__file__).parent
for line in (HERE.parent.parent / "backend/.env").read_text().splitlines():
    if line.strip() and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

from openai import AsyncOpenAI  # noqa: E402

client = AsyncOpenAI()
EMB_MODEL = "text-embedding-3-small"

CLASS_OF = {
    "lost_luggage": "sop1",
    "flight_info": "sop2",
    "transport_parking": "sop3",
    "wayfinding": "sop3",
}
EXEMPLAR_FRACTION = 0.2
MAX_EXEMPLARS_PER_CLASS = 80


def first_utterance(r: dict) -> str | None:
    for t in r["turns"]:
        if t["s"] == "Client" and len(t["t"].strip()) >= 15:
            return t["t"].strip()[:300]
    return None


def is_exemplar(dialogue_id: str) -> bool:
    return int(hashlib.sha1(dialogue_id.encode()).hexdigest(), 16) % 100 < EXEMPLAR_FRACTION * 100


async def embed_all(texts: list[str]) -> list[list[float]]:
    out: list[list[float]] = []
    for i in range(0, len(texts), 512):
        res = await client.embeddings.create(model=EMB_MODEL, input=texts[i : i + 512])
        out.extend(d.embedding for d in res.data)
    return out


def cos(a: list[float], b: list[float]) -> float:
    num = sum(x * y for x, y in zip(a, b))
    da = sum(x * x for x in a) ** 0.5
    db = sum(x * x for x in b) ** 0.5
    return num / (da * db) if da and db else 0.0


async def main() -> None:
    per = {d["id"]: d for d in json.load((HERE / "intents.json").open())["per_dialogue"]}
    rows = []
    for ln in (HERE / "cleaned.jsonl").open(encoding="utf-8"):
        r = json.loads(ln)
        utt = first_utterance(r)
        if not utt:
            continue
        lab = per.get(r["id"])
        primary = lab["labels"][0] if lab and lab["labels"] else None
        rows.append({"id": r["id"], "utt": utt, "cls": CLASS_OF.get(primary, "oos")})

    exemplars: dict[str, list[str]] = {"sop1": [], "sop2": [], "sop3": []}
    eval_rows = []
    for r in rows:
        if r["cls"] != "oos" and is_exemplar(r["id"]) and len(exemplars[r["cls"]]) < MAX_EXEMPLARS_PER_CLASS:
            exemplars[r["cls"]].append(r["utt"])
        else:
            eval_rows.append(r)

    print({k: len(v) for k, v in exemplars.items()}, "| eval:", Counter(r["cls"] for r in eval_rows))

    ex_texts = [(c, t) for c, ts in exemplars.items() for t in ts]
    ex_embs = await embed_all([t for _, t in ex_texts])
    ev_embs = await embed_all([r["utt"] for r in eval_rows])

    pools: dict[str, list[list[float]]] = {}
    for (c, _), e in zip(ex_texts, ex_embs):
        pools.setdefault(c, []).append(e)

    scored = []
    for r, e in zip(eval_rows, ev_embs):
        scores = {c: max(cos(e, x) for x in pool) for c, pool in pools.items()}
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        scored.append({**r, "top": ranked[0][0], "top_sim": round(ranked[0][1], 4),
                       "margin": round(ranked[0][1] - ranked[1][1], 4)})

    in_scope = [s for s in scored if s["cls"] != "oos"]
    oos = [s for s in scored if s["cls"] == "oos"]
    table = []
    for tau in (0.0, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50):
        routed = [s for s in in_scope if s["top_sim"] >= tau]
        acc = sum(1 for s in routed if s["top"] == s["cls"]) / len(routed) if routed else 0
        coverage = len(routed) / len(in_scope)
        oos_rejected = sum(1 for s in oos if s["top_sim"] < tau) / len(oos) if oos else 0
        table.append({"tau": tau, "in_scope_coverage": round(coverage, 3), "routed_accuracy": round(acc, 3),
                      "oos_rejection": round(oos_rejected, 3)})
        print(f"τ={tau:.2f}  coverage {coverage:5.1%}  accuracy@routed {acc:5.1%}  OOS rejected {oos_rejected:5.1%}")

    conf = Counter((s["cls"], s["top"]) for s in in_scope)
    print("confusion (gold→routed):", dict(conf))
    (HERE / "router_measurement.json").write_text(json.dumps(
        {"exemplars": {k: len(v) for k, v in exemplars.items()},
         "eval_counts": dict(Counter(r["cls"] for r in eval_rows)),
         "operating_table": table,
         "confusion": {f"{a}->{b}": n for (a, b), n in conf.items()}}, indent=1))


if __name__ == "__main__":
    asyncio.run(main())

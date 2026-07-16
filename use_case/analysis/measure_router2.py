#!/usr/bin/env python3
"""Router measurement v2 — two fixes and one alternative.

(a) embedding kNN, but the router input is the first TWO substantive client
    utterances (intake mode accumulates before committing), and exemplars are
    filtered to utterances that actually carry their theme's keywords;
(b) cheap-LLM router (gpt-4o-mini, one call, 4-way incl. out-of-scope) on a
    stratified 600-dialogue sample of the same eval split.

Same weak gold labels as v1 (whole-dialogue lexicon) — results remain floors.
"""
from __future__ import annotations

import asyncio
import hashlib
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

import intents as intent_mod  # noqa: E402  (reuse lexicon + norm)

client = AsyncOpenAI()
EMB_MODEL = "text-embedding-3-small"
CLASS_OF = {"lost_luggage": "sop1", "flight_info": "sop2", "transport_parking": "sop3", "wayfinding": "sop3"}
THEME_OF = {"sop1": {"lost_luggage"}, "sop2": {"flight_info"}, "sop3": {"transport_parking", "wayfinding"}}

LLM_SYS = (
    "You route the OPENING of a Málaga airport information-desk conversation to a procedure. Classes:\n"
    "sop1 = lost/delayed CHECKED LUGGAGE (suitcase didn't arrive, baggage office)\n"
    "sop2 = flight/check-in/boarding info (counters, gates, times, boarding passes)\n"
    "sop3 = ground transport, parking, or where-is-X wayfinding in the airport\n"
    "oos  = anything else (lost personal items, police/documents, shops, smalltalk, unintelligible)\n"
    'Return JSON {"route": "sop1|sop2|sop3|oos"}.'
)


def first_utts(r: dict, n: int = 2) -> str | None:
    utts = [t["t"].strip() for t in r["turns"] if t["s"] == "Client" and len(t["t"].strip()) >= 15]
    return " ".join(utts[:n])[:400] if utts else None


def is_exemplar(did: str) -> bool:
    return int(hashlib.sha1(did.encode()).hexdigest(), 16) % 100 < 20


def theme_in_text(theme_labels: set[str], text: str) -> bool:
    nt = intent_mod.norm(text)
    return any(any(intent_mod.norm(t) in nt for t in intent_mod.LEXICON[lb]) for lb in theme_labels)


async def embed_all(texts: list[str]) -> list[list[float]]:
    out: list[list[float]] = []
    for i in range(0, len(texts), 512):
        res = await client.embeddings.create(model=EMB_MODEL, input=texts[i : i + 512])
        out.extend(d.embedding for d in res.data)
    return out


def cos(a, b):
    num = sum(x * y for x, y in zip(a, b))
    return num / ((sum(x * x for x in a) ** 0.5) * (sum(y * y for y in b) ** 0.5) or 1)


async def llm_route(text: str, sem: asyncio.Semaphore) -> str:
    async with sem:
        res = await client.chat.completions.create(
            model="gpt-4o-mini", temperature=0, max_tokens=20,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": LLM_SYS}, {"role": "user", "content": text}],
        )
        try:
            return json.loads(res.choices[0].message.content).get("route", "oos")
        except json.JSONDecodeError:
            return "oos"


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

    exemplars: dict[str, list[str]] = {"sop1": [], "sop2": [], "sop3": []}
    eval_rows = []
    for r in rows:
        if r["cls"] != "oos" and is_exemplar(r["id"]) and len(exemplars[r["cls"]]) < 80:
            if theme_in_text(THEME_OF[r["cls"]], r["utt"]):  # exemplar must carry its theme
                exemplars[r["cls"]].append(r["utt"])
                continue
        eval_rows.append(r)

    # ---- (a) embedding kNN
    ex_texts = [(c, t) for c, ts in exemplars.items() for t in ts]
    ex_embs = await embed_all([t for _, t in ex_texts])
    ev_embs = await embed_all([r["utt"] for r in eval_rows])
    pools: dict[str, list] = {}
    for (c, _), e in zip(ex_texts, ex_embs):
        pools.setdefault(c, []).append(e)
    in_scope_n = correct = 0
    for r, e in zip(eval_rows, ev_embs):
        if r["cls"] == "oos":
            continue
        top = max(pools, key=lambda c: max(cos(e, x) for x in pools[c]))
        in_scope_n += 1
        correct += top == r["cls"]
    print(f"(a) embedding kNN, 2-utterance input, filtered exemplars: accuracy {correct/in_scope_n:.1%} on {in_scope_n}")

    # ---- (b) cheap-LLM router on stratified sample
    rng = random.Random(7)
    sample: list[dict] = []
    for cls in ("sop1", "sop2", "sop3", "oos"):
        pool = [r for r in eval_rows if r["cls"] == cls]
        sample += rng.sample(pool, min(150, len(pool)))
    sem = asyncio.Semaphore(16)
    preds = await asyncio.gather(*(llm_route(r["utt"], sem) for r in sample))
    in_s = [(r, p) for r, p in zip(sample, preds) if r["cls"] != "oos"]
    oos = [(r, p) for r, p in zip(sample, preds) if r["cls"] == "oos"]
    acc = sum(1 for r, p in in_s if p == r["cls"]) / len(in_s)
    oos_rej = sum(1 for r, p in oos if p == "oos") / len(oos)
    conf = Counter((r["cls"], p) for r, p in in_s)
    print(f"(b) gpt-4o-mini router: in-scope accuracy {acc:.1%} on {len(in_s)} · OOS rejected {oos_rej:.1%} on {len(oos)}")
    print("    confusion:", dict(conf))
    (HERE / "router_measurement_v2.json").write_text(json.dumps(
        {"knn_2utt_accuracy": round(correct / in_scope_n, 4), "knn_eval_n": in_scope_n,
         "llm_accuracy": round(acc, 4), "llm_n": len(in_s), "llm_oos_rejection": round(oos_rej, 4),
         "llm_confusion": {f"{a}->{b}": n for (a, b), n in conf.items()}}, indent=1))


if __name__ == "__main__":
    asyncio.run(main())

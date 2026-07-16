#!/usr/bin/env python3
"""Iteration 1: mine the fact base from ALL agent answers in the corpus.

v1 used only the 108 SOP-mining dialogues → 25 facts; the replay misses were
dominated by absent desk knowledge. This pass scans every cleaned dialogue's
agent turns for informative answers (location/number/direction cues), dedupes,
batches them through gpt-4o into atomic facts, then merges + dedupes fact keys
in one reduce call. Push replaces the airport_facts corpus content.

Usage: backend/.venv/bin/python mine_facts_full.py [--push]
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import unicodedata
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
for line in (HERE.parent.parent / "backend/.env").read_text().splitlines():
    if line.strip() and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

from openai import AsyncOpenAI  # noqa: E402

client = AsyncOpenAI()

CUES = re.compile(
    r"\b(cinta|mostrador|oficina|puerta|planta|piso|salida|llegadas?|terminal|parking|aparcamiento|"
    r"autobus|autobús|tren|taxi|euros?|minutos?|horas?|derecha|izquierda|detras|detrás|enfrente|frente|"
    r"al fondo|arriba|abajo|ascensor|escalera|belt|counter|office|gate|floor|exit|arrivals|departures|"
    r"bus|train|right|left|behind|front|upstairs|downstairs|elevator|ground force|avia|menzies|"
    r"la manon|cercanias|cercanías|renfe)\b",
    re.IGNORECASE,
)

MAP_SYS = (
    "You extract atomic facts from real answers given by Málaga Airport (AGP) information-desk agents "
    "(noisy speech-to-text; Spanish/English). Return JSON {\"facts\": [{\"key\": \"<kebab-slug>\", "
    "\"topic\": \"<baggage|flights|transport|parking|wayfinding|services>\", \"text\": \"<one atomic fact in "
    "English, Spanish phrasing in parentheses>\"}]}.\n"
    "Only extract REUSABLE airport facts (locations, procedures, schedules, prices, rules). Skip one-off "
    "conversation specifics (a particular passenger's flight time), vague fragments, and anything you cannot state "
    "confidently from the input. Fix obvious ASR garbling of brand names (Ground Force, Avia Partner, Menzies, "
    "La Manon, Renfe). Do not invent."
)

REDUCE_SYS = (
    "You merge fact lists mined from many conversations at Málaga Airport into ONE deduplicated fact base. "
    "Return JSON {\"facts\": [...]} with the same item schema. Merge duplicates and near-duplicates into the most "
    "specific, most frequently supported version; when versions genuinely conflict, keep the more specific one and "
    "append '(reported variably)' to the text. Keys must be unique kebab-case slugs. Do not invent facts."
)


def norm(t: str) -> str:
    t = unicodedata.normalize("NFKD", t.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", t)).strip()


def candidates() -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for ln in (HERE / "cleaned.jsonl").open(encoding="utf-8"):
        r = json.loads(ln)
        for t in r["turns"]:
            if t["s"] != "Agent":
                continue
            text = t["t"].strip()
            if not (25 <= len(text) <= 300) or not CUES.search(text):
                continue
            key = norm(text)[:90]
            if key in seen:
                continue
            seen.add(key)
            out.append(text)
    return out


async def mine() -> list[dict]:
    cands = candidates()
    print(f"{len(cands)} candidate agent answers")
    BATCH = 90
    batches = [cands[i : i + BATCH] for i in range(0, min(len(cands), 1440), BATCH)]
    sem = asyncio.Semaphore(4)

    async def run_map(batch: list[str]) -> list[dict]:
        async with sem:
            res = await client.chat.completions.create(
                model="gpt-4o", temperature=0.1, max_tokens=3500,
                response_format={"type": "json_object"},
                messages=[{"role": "system", "content": MAP_SYS},
                          {"role": "user", "content": json.dumps(batch, ensure_ascii=False)}],
            )
            try:
                return json.loads(res.choices[0].message.content).get("facts", [])
            except json.JSONDecodeError:
                return []

    mapped = await asyncio.gather(*(run_map(b) for b in batches))
    raw = [f for fs in mapped for f in fs]
    print(f"{len(raw)} raw facts from {len(batches)} batches")
    res = await client.chat.completions.create(
        model="gpt-4o", temperature=0.1, max_tokens=8000,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": REDUCE_SYS},
                  {"role": "user", "content": json.dumps(raw, ensure_ascii=False)}],
    )
    facts = json.loads(res.choices[0].message.content)["facts"]
    # unique keys
    seen: set[str] = set()
    final = []
    for f in facts:
        k = f.get("key", "")
        if not k or k in seen or not f.get("text"):
            continue
        seen.add(k)
        final.append(f)
    with (HERE / "facts_full.jsonl").open("w", encoding="utf-8") as out:
        for f in final:
            out.write(json.dumps(f, ensure_ascii=False) + "\n")
    print(f"{len(final)} deduplicated facts written")
    return final


def push(facts: list[dict]) -> None:
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer sop_19b6179b1c913671df2251e9ac2eb70d9b817c91",
        "X-Project": "malaga",
    }

    def call(method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request("http://127.0.0.1:8100" + path,
                                     data=json.dumps(body).encode() if body else None,
                                     method=method, headers=headers)
        return json.loads(urllib.request.urlopen(req, timeout=600).read())

    docs = [{"doc_key": f["key"], "topic": f["topic"], "tags": [f["topic"]], "text": f["text"]} for f in facts]
    for i in range(0, len(docs), 100):
        print(call("PUT", "/corpora/airport_facts/docs", {"docs": docs[i : i + 100]}))


if __name__ == "__main__":
    facts = asyncio.run(mine())
    if "--push" in sys.argv:
        push(facts)

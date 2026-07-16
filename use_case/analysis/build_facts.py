#!/usr/bin/env python3
"""Distill the mined canonical answers into an airport-facts corpus (LOCAL).

One gpt-4o call: dedupe/normalize the canonical_answers + data_needed from the
9 map batches into atomic bilingual fact documents. Facts derive only from
what real agents said repeatedly — nothing invented. Output facts.jsonl
(gitignored), then load into the aena/malaga corpus via the /corpora API.

Usage: backend/.venv/bin/python build_facts.py [--push]
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

SYS = (
    "You normalize field notes from a real airport information desk (Málaga Airport, AGP) into an atomic fact base "
    "for retrieval. Input: canonical answers extracted from many real conversations (may contain ASR noise and "
    "duplicates). Output JSON: {\"facts\": [{\"key\": \"<kebab-case-slug>\", \"topic\": \"<baggage|flights|"
    "transport|parking|wayfinding|services>\", \"text\": \"<ONE atomic, retrieval-friendly fact stating the "
    "answer in English, then the Spanish phrasing in parentheses>\"}]}.\n"
    "Rules: one fact per location/answer; merge duplicates (prefer the most frequent/specific version); fix obvious "
    "ASR garbling of brand names (e.g. Ground Force, Avia Partner, Menzies) but do NOT invent facts, numbers or "
    "locations that are not in the input; skip vague or contradictory items."
)


async def build() -> list[dict]:
    material = []
    for f in sorted((HERE / "mined").glob("map_*.json")):
        for batch in json.loads(f.read_text()):
            b = batch if isinstance(batch, dict) else json.loads(batch)
            material.append(
                {"canonical_answers": b.get("canonical_answers", []), "data_needed": b.get("data_needed", [])}
            )
    client = AsyncOpenAI()
    res = await client.chat.completions.create(
        model="gpt-4o",
        temperature=0.1,
        max_tokens=4000,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": SYS}, {"role": "user", "content": json.dumps(material, ensure_ascii=False)}],
    )
    facts = json.loads(res.choices[0].message.content)["facts"]
    with (HERE / "facts.jsonl").open("w", encoding="utf-8") as out:
        for f in facts:
            out.write(json.dumps(f, ensure_ascii=False) + "\n")
    print(f"{len(facts)} facts written")
    return facts


def push(facts: list[dict]) -> None:
    key = next(
        ln.split("sop_")[1].strip()
        for ln in (HERE.parent.parent / "TENANT_KEYS.local.txt").read_text().splitlines()
        if "key:" in ln and "aena" in open(HERE.parent.parent / "TENANT_KEYS.local.txt").read().split(ln)[0][-300:]
    )
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer sop_{key}", "X-Project": "malaga"}

    def call(method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(
            "http://127.0.0.1:8100" + path,
            data=json.dumps(body).encode() if body else None, method=method, headers=headers,
        )
        return json.loads(urllib.request.urlopen(req, timeout=300).read())

    print(call("PUT", "/corpora/airport_facts"))
    docs = [{"doc_key": f["key"], "topic": f["topic"], "tags": [f["topic"]], "text": f["text"]} for f in facts]
    print(call("PUT", "/corpora/airport_facts/docs", {"docs": docs}))


if __name__ == "__main__":
    facts = asyncio.run(build())
    if "--push" in sys.argv:
        push(facts)

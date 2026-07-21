#!/usr/bin/env python3
"""Mine written SOP documents from the cleaned AENA corpus (map-reduce, gpt-4o).

Per theme: pick the cleanest substantive dialogues, summarize agent behavior in
batches (MAP), then synthesize ONE written operating procedure (REDUCE) shaped
like a document a desk supervisor would hand to a new hire. The output .txt
files then go through SOPilot's own /sops/ingest — the product's normal
text→SOP path — so the POC exercises the real pipeline end to end.

Customer cleared the corpus for LLM processing (no critical data, publicly
available information — decision 2026-07-16). Outputs land in
use_case/analysis/mined/ (gitignored — derived from customer data).

Usage: backend/.venv/bin/python mine_sop.py [theme ...]
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).parent
MINED = HERE / "mined"
MINED.mkdir(exist_ok=True)

# export OPENAI_API_KEY from backend/.env
for line in (HERE.parent.parent / "backend/.env").read_text().splitlines():
    if line.strip() and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

from openai import AsyncOpenAI  # noqa: E402

client = AsyncOpenAI()
MODEL = "gpt-4o"

THEMES = {
    "lost_luggage": {
        "labels": {"lost_luggage"},
        "title": "Lost or Delayed Luggage — Information Desk Procedure (Málaga Airport)",
    },
    "flight_info": {
        "labels": {"flight_info"},
        "title": "Flight, Check-in and Boarding Information — Information Desk Procedure (Málaga Airport)",
    },
    "transport_wayfinding": {
        "labels": {"transport_parking", "wayfinding"},
        "title": "Ground Transport, Parking and Wayfinding — Information Desk Procedure (Málaga Airport)",
    },
    "airport_services": {
        "labels": {"airport_services"},
        "title": "Airport Services and Facilities — Information Desk Procedure (Málaga Airport)",
    },
}

BATCHES = 3
PER_BATCH = 12

MAP_SYS = (
    "You analyze real airport information-desk conversations (Spanish/English, imperfect speech-to-text — expect "
    "garbled proper nouns and occasional mislabeled speakers; trust turn ORDER over labels). Extract how the human "
    "agent handles this category of request. Return JSON:\n"
    '{"steps": [ordered recurring procedure steps the agent follows],\n'
    ' "decision_points": [conditions that change the path, e.g. which airline/handler],\n'
    ' "data_needed": [external facts the agent looks up or must know],\n'
    ' "canonical_answers": [recurring factual answers/directions, cleaned of ASR noise],\n'
    ' "phrases": [useful agent phrasings worth reusing, Spanish and English],\n'
    ' "endings": [how conversations typically conclude, incl. failure/handoff cases]}'
)

REDUCE_SYS = (
    "You are writing a Standard Operating Procedure for the Málaga Airport information desk, synthesized from "
    "analyses of many real conversations. Write ONE plain-text procedure document a desk supervisor would hand to "
    "a new agent. Requirements:\n"
    "- structure: purpose; the conversation flow as numbered stages (greeting → identify need → verify what's "
    "needed → resolve or route → close), with the decision points and branches the analyses show;\n"
    "- each stage: what the agent must do, must ask, and must say (include the best canonical directions/answers; "
    "give key phrases in both Spanish and English);\n"
    "- explicit outcomes: resolved, routed to another desk/party (say which), caller gives up;\n"
    "- external data the agent needs at specific stages (mark clearly, e.g. 'LOOKUP: flight status by number');\n"
    "- do NOT invent facts not present in the analyses; where analyses conflict, prefer the most frequent version;\n"
    "- concise: 500-800 words, plain text, no markdown tables."
)


def pick_dialogues(labels: set[str]) -> list[dict]:
    per = {d["id"]: d for d in json.load((HERE / "intents.json").open())["per_dialogue"]}
    rows = []
    for ln in (HERE / "cleaned.jsonl").open(encoding="utf-8"):
        r = json.loads(ln)
        lab = per.get(r["id"])
        if not lab or not lab["labels"] or lab["labels"][0] not in labels:
            continue
        n = r["stats"]["turns"]
        if not (5 <= n <= 20):
            continue
        # quality: substantive but low-echo
        score = min(n, 14) - 2.0 * (r["stats"]["echo_removed"] / max(1, n + r["stats"]["echo_removed"]))
        rows.append((score, r))
    rows.sort(key=lambda x: -x[0])
    return [r for _, r in rows[: BATCHES * PER_BATCH]]


def render(r: dict) -> str:
    return "\n".join(f"{t['s']}: {t['t']}" for t in r["turns"])


async def chat(system: str, user: str, *, json_mode: bool) -> str:
    res = await client.chat.completions.create(
        model=MODEL,
        temperature=0.2,
        max_tokens=2500,
        response_format={"type": "json_object"} if json_mode else {"type": "text"},
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    )
    return res.choices[0].message.content or ""


async def mine(theme: str, cfg: dict) -> None:
    dialogues = pick_dialogues(cfg["labels"])
    print(f"[{theme}] {len(dialogues)} dialogues selected")
    batches = [dialogues[i::BATCHES] for i in range(BATCHES)]
    maps = await asyncio.gather(
        *(
            chat(
                MAP_SYS,
                f"Category: {cfg['title']}\n\n"
                + "\n\n---\n\n".join(f"CONVERSATION {i + 1}:\n{render(r)}" for i, r in enumerate(batch)),
                json_mode=True,
            )
            for batch in batches
            if batch
        )
    )
    (MINED / f"map_{theme}.json").write_text("[\n" + ",\n".join(maps) + "\n]")
    doc = await chat(
        REDUCE_SYS,
        f"TITLE: {cfg['title']}\n\nANALYSES OF {len(dialogues)} REAL CONVERSATIONS (in {len(maps)} batches):\n\n"
        + "\n\n===\n\n".join(maps),
        json_mode=False,
    )
    out = MINED / f"sop_{theme}.txt"
    out.write_text(cfg["title"] + "\n\n" + doc.strip() + "\n")
    print(f"[{theme}] written {out} ({len(doc)} chars)")


async def main() -> None:
    themes = sys.argv[1:] or list(THEMES)
    for t in themes:
        await mine(t, THEMES[t])


if __name__ == "__main__":
    asyncio.run(main())

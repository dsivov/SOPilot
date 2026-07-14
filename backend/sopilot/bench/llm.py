"""Minimal OpenAI chat helpers for the bench harness (never used by the runtime)."""
from __future__ import annotations

import json
import os

from openai import AsyncOpenAI

_client: AsyncOpenAI | None = None

DEFAULT_MODEL = os.environ.get("SOPILOT_BENCH_MODEL", "gpt-4o-mini")


def client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI()
    return _client


async def chat_text(system: str, messages: list[dict], *, model: str = "") -> str:
    res = await client().chat.completions.create(
        model=model or DEFAULT_MODEL,
        messages=[{"role": "system", "content": system}, *messages],
        temperature=0.7,
        max_tokens=220,
    )
    return (res.choices[0].message.content or "").strip()


async def chat_json(system: str, messages: list[dict], *, model: str = "") -> dict:
    res = await client().chat.completions.create(
        model=model or DEFAULT_MODEL,
        messages=[{"role": "system", "content": system}, *messages],
        temperature=0.3,
        max_tokens=200,
        response_format={"type": "json_object"},
    )
    try:
        return json.loads(res.choices[0].message.content or "{}")
    except json.JSONDecodeError:
        return {}

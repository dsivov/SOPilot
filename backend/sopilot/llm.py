"""LLM helper for AUTHORING-time work (SOP builder/ingestion). Never imported by
the runtime turn path — the online lane stays LLM-free by architecture.
"""
from __future__ import annotations

import json

from openai import AsyncOpenAI

from .config import get_settings

_client: AsyncOpenAI | None = None


def client() -> AsyncOpenAI:
    global _client
    if _client is None:
        get_settings()  # ensures .env is exported to the process env
        _client = AsyncOpenAI()
    return _client


async def chat_json(system: str, user: str, *, model: str | None = None, max_tokens: int = 4000) -> dict:
    res = await client().chat.completions.create(
        model=model or get_settings().builder_model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.2,
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
    )
    try:
        return json.loads(res.choices[0].message.content or "{}")
    except json.JSONDecodeError:
        return {}

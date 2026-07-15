"""Voice channel (P2): OpenAI Realtime steering, ported from the research
prototype's proven pattern.

- The API key never leaves the server: the browser gets a short-lived ephemeral
  client secret minted here.
- The realtime session runs with MANUAL turn control (no auto-response): the
  beat between "caller stopped speaking" and "agent speaks" is where the
  supervisor injects the current stage's instructions (session.update) and then
  triggers response.create. The speech model never sees the whole SOP.
- voice-turn = the converse pipeline minus the responder (the realtime model
  does the talking): classify+propose → plan-turn → instruction payload back.
"""
from __future__ import annotations

import json
import urllib.request

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_db
from ..models import SopVersion, Turn
from ..schemas import TaskDefinition
from ..tenancy import Scope, resolve_scope
from .runtime import PlanTurnRequest, _get_session, plan_turn

router = APIRouter(prefix="/sessions", tags=["voice"])


class VoiceTurnRequest(BaseModel):
    user_message: str  # final transcript of the caller's utterance
    prev_assistant_message: str | None = None  # transcript of what the agent last said


def _mint_client_secret(api_key: str) -> dict:
    """Mint an ephemeral Realtime client secret (GA endpoint, beta fallback)."""
    settings = get_settings()
    ga_body = {
        "session": {
            "type": "realtime",
            "model": settings.realtime_model,
            "audio": {
                "input": {
                    "transcription": {"model": "whisper-1"},
                    "turn_detection": {"type": "server_vad", "create_response": False},
                },
                "output": {"voice": settings.realtime_voice},
            },
        }
    }
    beta_body = {
        "model": settings.realtime_model,
        "voice": settings.realtime_voice,
        "input_audio_transcription": {"model": "whisper-1"},
        "turn_detection": {"type": "server_vad", "create_response": False},
    }
    attempts = [
        ("https://api.openai.com/v1/realtime/client_secrets", ga_body, "ga"),
        ("https://api.openai.com/v1/realtime/sessions", beta_body, "beta"),
    ]
    last_err = ""
    for url, body, flavor in attempts:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            method="POST",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                data = json.loads(res.read())
                secret = data.get("value") or (data.get("client_secret") or {}).get("value")
                if secret:
                    return {"client_secret": secret, "api_flavor": flavor, "raw": data}
                last_err = f"{flavor}: no client secret in response"
        except urllib.error.HTTPError as e:
            last_err = f"{flavor} {e.code}: {e.read().decode()[:200]}"
        except Exception as e:  # noqa: BLE001
            last_err = f"{flavor}: {e}"
    raise HTTPException(status_code=502, detail=f"could not mint realtime secret — {last_err}")


@router.post("/{session_id}/realtime-token")
async def realtime_token(
    session_id: str,
    scope: Scope = Depends(resolve_scope),
    db: AsyncSession = Depends(get_db),
) -> dict:
    import os

    session = await _get_session(db, scope, session_id)
    if session.status != "active":
        raise HTTPException(status_code=409, detail="session has ended")
    settings = get_settings()
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=503, detail="OPENAI_API_KEY not configured on the server")
    minted = _mint_client_secret(api_key)
    return {
        "client_secret": minted["client_secret"],
        "api_flavor": minted["api_flavor"],
        "model": settings.realtime_model,
        "webrtc_url_ga": f"https://api.openai.com/v1/realtime/calls?model={settings.realtime_model}",
        "webrtc_url_beta": f"https://api.openai.com/v1/realtime?model={settings.realtime_model}",
    }


@router.post("/{session_id}/voice-turn")
async def voice_turn(
    session_id: str,
    body: VoiceTurnRequest,
    request: Request,
    scope: Scope = Depends(resolve_scope),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Classify + plan for one voice turn; the realtime model speaks the result."""
    import time as _time

    from ..agent import classify_and_propose
    from ..sop_graph import SOPGraph

    t0 = _time.perf_counter()
    session = await _get_session(db, scope, session_id)
    if session.status != "active":
        raise HTTPException(status_code=409, detail="session has ended")
    version = (
        await db.execute(
            select(SopVersion).where(
                SopVersion.sop_id == session.sop_id, SopVersion.version == session.sop_version
            )
        )
    ).scalar_one()
    task_def = TaskDefinition.model_validate(version.definition)
    graph = SOPGraph(task_def)
    prior_turns = (
        (await db.execute(select(Turn).where(Turn.session_id == session.id).order_by(Turn.turn_index)))
        .scalars()
        .all()
    )
    history: list[dict] = []
    for t in prior_turns:
        if t.user_message:
            history.append({"role": "user", "content": t.user_message})
        if t.assistant_message:
            history.append({"role": "assistant", "content": t.assistant_message})

    visited = graph.visited_from_history(
        [{"action": t.action} for t in prior_turns], [t.state for t in prior_turns if t.state]
    )
    allowed = graph.allowed_actions(visited)
    proposal = await classify_and_propose(
        task_def, history, body.user_message, allowed,
        prior_cohort=prior_turns[-1].cohort if prior_turns else "",
    )
    plan = await plan_turn(
        session_id,
        PlanTurnRequest(
            user_message=body.user_message,
            cohort=proposal["cohort"],
            mood=proposal["mood"],
            state=proposal["state"],
            action=proposal["action"] or None,
            prev_assistant_message=body.prev_assistant_message,
        ),
        request,
        scope,
        db,
    )
    cp = task_def.conversation_profile
    terminal = (
        "success" if proposal["state"] in set(cp.success_markers)
        else "failure" if proposal["state"] in set(cp.failure_markers)
        else None
    )
    # Voice instruction: the plan payload plus voice-specific delivery rules.
    instructions = (
        (plan["prompt_text"] or "Respond helpfully and professionally.")
        + "\n\nDELIVERY: You are speaking on a live phone call. Reply in 1-3 short natural sentences. "
        "Do not read lists or headings aloud; weave the facts into speech."
    )
    return {
        "instructions": instructions,
        "terminal": terminal,
        "classification": proposal,
        "turn": plan,
        "plan_ms": int((_time.perf_counter() - t0) * 1000),
    }

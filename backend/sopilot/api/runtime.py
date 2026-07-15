"""Online-lane runtime routes: plan-turn (the hot path) and session outcome."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..events import TurnEvent, publish_turn_event
from ..models import ConversationSession, PoolPickAudit, PrecedentTrace, SopVersion, Turn
from ..rerank import rerank_pool_for_turn
from ..runtime import build_plan, choose_action
from ..schemas import TaskDefinition
from ..sop_graph import SOPGraph
from ..tenancy import Scope, resolve_scope

router = APIRouter(prefix="/sessions", tags=["runtime"])


class PlanTurnRequest(BaseModel):
    user_message: str
    # P2 wires a real classifier; until then the caller may supply these.
    cohort: str = ""
    mood: str = ""
    state: str = ""
    action: Optional[str] = None  # explicit action override (must be SOP-legal)
    # Backfill of what the agent actually said last turn (for history + traces).
    prev_assistant_message: Optional[str] = None


class EndSessionRequest(BaseModel):
    outcome: Optional[str] = None  # success | failure | abandoned


TERMINAL_REWARDS = {"success": 1.0, "abandoned": 0.25, "failure": 0.0}


async def _get_session(db: AsyncSession, scope: Scope, session_id: str) -> ConversationSession:
    row = (
        await db.execute(
            select(ConversationSession).where(
                ConversationSession.id == session_id,
                ConversationSession.tenant_id == scope.tenant_id,
                ConversationSession.project_id == scope.project_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    return row


@router.post("/{session_id}/plan-turn")
async def plan_turn(
    session_id: str,
    body: PlanTurnRequest,
    request: Request,
    scope: Scope = Depends(resolve_scope),
    db: AsyncSession = Depends(get_db),
) -> dict:
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
    turn_index = len(prior_turns)
    if body.prev_assistant_message and prior_turns:
        await db.execute(
            update(Turn)
            .where(Turn.id == prior_turns[-1].id)
            .values(assistant_message=body.prev_assistant_message)
        )

    history = [{"action": t.action} for t in prior_turns]
    state_log = [t.state for t in prior_turns if t.state] + ([body.state] if body.state else [])
    visited = graph.visited_from_history(history, state_log)
    allowed = graph.allowed_actions(visited)
    chosen = choose_action(body.action, allowed)
    if body.action and body.action not in allowed:
        raise HTTPException(
            status_code=422,
            detail={"message": f"action '{body.action}' is not SOP-legal now", "allowed": allowed},
        )

    picks = []
    rerank = None
    dep_payloads: dict[str, str] = {}
    consume_stats: dict = {}
    instruction_item = None
    pool = request.app.state.pool
    if scope.retrieval_enabled:
        pool_items = await pool.get_pool(scope, session.id)
        rerank = await rerank_pool_for_turn(
            pool_items,
            live_user_message=body.user_message,
            embedder=request.app.state.embedder,
            query_emb=getattr(request.state, "query_emb", None),
        )
        picks = rerank.picks
        if scope.sop_enabled:
            instruction_item = await pool.lookup_instruction(
                scope, session.id, chosen_action=chosen, classified_state=body.state
            )
            if instruction_item is not None and instruction_item.fetch_id:
                await request.app.state.prefetch._mark_consumed(
                    scope, instruction_item.fetch_id, turn_index
                )
        dep_payloads, consume_stats = await request.app.state.prefetch.consume(
            scope=scope,
            session_id=session.id,
            task_def=task_def,
            action_name=chosen,
            current_turn_index=turn_index,
        )
        db.add(
            PoolPickAudit(
                tenant_id=scope.tenant_id,
                project_id=scope.project_id,
                session_id=session.id,
                turn_index=turn_index,
                picked_item_ids=[p.item_id for p in picks],
                pool_size_at_pick=len(pool_items),
                rationale=rerank.rationale,
                pick_duration_ms=rerank.duration_ms,
            )
        )
    elif scope.sop_enabled:
        # SOP-only mode: no speculation, no pool — resolve declared deps live.
        dep_payloads, consume_stats = await request.app.state.prefetch.consume(
            scope=scope,
            session_id=session.id,
            task_def=task_def,
            action_name=chosen,
            current_turn_index=turn_index,
            await_inflight_ms=0,
        )

    # D-7: stage blocks come from the bindings pinned at session start.
    bindings = session.prompt_bindings or {}
    action_obj = next((a for a in task_def.agent_actions if a.name == chosen), None)
    stage_blocks = [
        bindings[n]["content"]
        for n in (action_obj.prompt_blocks if action_obj else [])
        if n in bindings
    ]

    plan = build_plan(
        turn_index=turn_index,
        subsystems=scope.subsystems,
        task_def=task_def,
        allowed_actions=allowed,
        chosen_action=chosen,
        picks=picks,
        rerank=rerank,
        dep_payloads=dep_payloads,
        consume_stats=consume_stats,
        instruction_item=instruction_item,
        stage_blocks=stage_blocks,
    )

    db.add(
        Turn(
            session_id=session.id,
            turn_index=turn_index,
            user_message=body.user_message,
            cohort=body.cohort,
            mood=body.mood,
            state=body.state,
            action=chosen,
            instruction_hit=plan.instruction_hit,
        )
    )
    db.add(
        PrecedentTrace(
            tenant_id=scope.tenant_id,
            project_id=scope.project_id,
            sop_id=session.sop_id,
            session_id=session.id,
            turn_index=turn_index,
            cohort=body.cohort,
            mood=body.mood,
            action=chosen,
            immediate_state=body.state,
        )
    )
    await db.commit()

    await publish_turn_event(
        request.app.state.redis,
        TurnEvent(
            tenant_id=scope.tenant_id,
            project_id=scope.project_id,
            subsystems=scope.subsystems,
            session_id=session.id,
            sop_id=session.sop_id,
            sop_version=session.sop_version,
            turn_index=turn_index,
            user_message=body.user_message,
            cohort=body.cohort,
            mood=body.mood,
            state=body.state,
            action=chosen,
        ),
    )

    return {
        "turn_index": plan.turn_index,
        "chosen_action": plan.chosen_action,
        "allowed_actions": plan.allowed_actions,
        "subsystems": plan.subsystems,
        "prompt_text": plan.prompt_text,
        "context_block": plan.context_block,
        "instruction_hit": plan.instruction_hit,
        "picks": plan.picks,
        "consume_stats": plan.consume_stats,
        "rerank_ms": plan.rerank_ms,
    }


class ConverseRequest(BaseModel):
    user_message: str


@router.post("/{session_id}/converse")
async def converse(
    session_id: str,
    body: ConverseRequest,
    request: Request,
    scope: Scope = Depends(resolve_scope),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Full text-channel turn: classify+propose (one strong-model call) →
    plan-turn (pool, prompts, event) → respond from the instruction payload.
    The voice channel reuses everything except the respond step."""
    import time as _time

    from ..agent import classify_and_propose, respond
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

    async def _embed_query():
        try:
            return await request.app.state.embedder.embed(body.user_message)
        except Exception:
            return None

    import asyncio as _asyncio

    proposal, query_emb = await _asyncio.gather(
        classify_and_propose(
            task_def, history, body.user_message, allowed,
            prior_cohort=prior_turns[-1].cohort if prior_turns else "",
        ),
        _embed_query(),
    )
    request.state.query_emb = query_emb

    plan = await plan_turn(
        session_id,
        PlanTurnRequest(
            user_message=body.user_message,
            cohort=proposal["cohort"],
            mood=proposal["mood"],
            state=proposal["state"],
            action=proposal["action"] or None,
        ),
        request,
        scope,
        db,
    )

    reply = plan["prompt_text"] if plan["instruction_hit"] else await respond(
        plan["prompt_text"], history, body.user_message
    )
    await db.execute(
        update(Turn)
        .where(Turn.session_id == session.id, Turn.turn_index == plan["turn_index"])
        .values(assistant_message=reply, duration_ms=int((_time.perf_counter() - t0) * 1000))
    )
    await db.commit()

    cp = task_def.conversation_profile
    terminal = (
        "success" if proposal["state"] in set(cp.success_markers)
        else "failure" if proposal["state"] in set(cp.failure_markers)
        else None
    )
    return {
        "reply": reply,
        "terminal": terminal,
        "classification": proposal,
        "turn": plan,
        "total_ms": int((_time.perf_counter() - t0) * 1000),
    }


@router.post("/{session_id}/outcome")
async def record_outcome(
    session_id: str,
    body: EndSessionRequest,
    scope: Scope = Depends(resolve_scope),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Back-propagate the session outcome onto its precedent traces — this is what
    makes the predictor prefer action paths that historically ended well."""
    session = await _get_session(db, scope, session_id)
    outcome = body.outcome or "abandoned"
    if outcome not in TERMINAL_REWARDS:
        raise HTTPException(status_code=422, detail=f"outcome must be one of {list(TERMINAL_REWARDS)}")
    reward = TERMINAL_REWARDS[outcome]
    await db.execute(
        update(PrecedentTrace)
        .where(
            PrecedentTrace.tenant_id == scope.tenant_id,
            PrecedentTrace.session_id == session.id,
        )
        .values(terminal_outcome=outcome, terminal_reward=reward)
    )
    session.terminal_outcome = outcome
    await db.commit()
    return {"session_id": session.id, "outcome": outcome, "terminal_reward": reward}

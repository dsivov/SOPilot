"""Online-lane runtime routes: plan-turn (the hot path) and session outcome."""
from __future__ import annotations

from typing import Optional

import logging

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

log = logging.getLogger(__name__)

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
    if session.subsystems_override:
        from dataclasses import replace as _replace

        scope = _replace(scope, subsystems=session.subsystems_override)
    from ..config import get_settings as _gs

    limit = _gs().quota_turns_per_min
    if limit > 0 and not getattr(request.state, "quota_counted", False):
        request.state.quota_counted = True  # converse/voice call through here once
        n = await request.app.state.pool.count_turn(scope)
        if n > limit:
            raise HTTPException(
                status_code=429,
                detail=f"tenant turn quota exceeded ({limit}/min) — retry shortly",
            )
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
            user_text=body.user_message,
            cohort=body.cohort,
            mood=body.mood,
            state=body.state,
            query_emb=getattr(request.state, "query_emb", None),
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
            user_text=body.user_message,
            cohort=body.cohort,
            mood=body.mood,
            state=body.state,
            query_emb=getattr(request.state, "query_emb", None),
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

    # Feature E: persist what actually ran — the debugging/analysis record.
    debug = {
        "allowed_actions": allowed,
        "stage_blocks": stage_blocks,
        "prompt_text": plan.prompt_text,
        "context_block": plan.context_block,
        "instruction_hit": plan.instruction_hit,
        "retrieval": {
            name: str(payload)[:400] for name, payload in (dep_payloads or {}).items()
        },
        "picks": plan.picks,
        "consume_stats": plan.consume_stats,
        "rerank_ms": plan.rerank_ms,
    }
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
            debug=debug,
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

    log.info(
        "turn planned session=%s turn=%d action=%s state=%s subsystems=%s rerank_ms=%s picks=%d "
        "instruction_hit=%s consumed=%s",
        session.id, turn_index, chosen, body.state or "-", scope.subsystems,
        plan.rerank_ms, len(plan.picks or []), plan.instruction_hit, plan.consume_stats,
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


async def _routing_candidates(db: AsyncSession, scope: Scope) -> list[dict]:
    """Published SOPs of the project as router candidates (id, name, description)."""
    from ..models import Sop

    rows = (
        await db.execute(
            select(Sop, SopVersion)
            .join(SopVersion, SopVersion.sop_id == Sop.id)
            .where(
                Sop.tenant_id == scope.tenant_id,
                Sop.project_id == scope.project_id,
                SopVersion.status == "published",
            )
            .order_by(SopVersion.version.asc())
        )
    ).all()
    latest: dict[str, tuple] = {}
    for sop, ver in rows:  # ascending — last write wins = newest published
        latest[sop.id] = (sop, ver)
    return [
        {"id": sop.id, "name": sop.name, "description": (ver.definition or {}).get("description", ""), "version": ver.version}
        for sop, ver in latest.values()
    ]


async def _assign_sop(db: AsyncSession, scope: Scope, session, sop_id: str, version: int) -> None:
    """Route/switch a session onto an SOP: pin version + prompt bindings."""
    from ..runtime import collect_prompt_block_names
    from .prompt_blocks import resolve_published_blocks

    ver = (
        await db.execute(
            select(SopVersion).where(SopVersion.sop_id == sop_id, SopVersion.version == version)
        )
    ).scalar_one()
    task_def = TaskDefinition.model_validate(ver.definition)
    bindings, _missing = await resolve_published_blocks(db, scope, collect_prompt_block_names(task_def))
    session.sop_id = sop_id
    session.sop_version = version
    session.prompt_bindings = bindings or None


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
    if session.subsystems_override:
        from dataclasses import replace as _replace

        scope = _replace(scope, subsystems=session.subsystems_override)
    from ..config import get_settings as _gs

    limit = _gs().quota_turns_per_min
    if limit > 0 and not getattr(request.state, "quota_counted", False):
        request.state.quota_counted = True  # converse/voice call through here once
        n = await request.app.state.pool.count_turn(scope)
        if n > limit:
            raise HTTPException(
                status_code=429,
                detail=f"tenant turn quota exceeded ({limit}/min) — retry shortly",
            )
    routing_info: dict | None = None
    if session.sop_id is None:
        # D-11 intake: route on the client utterances gathered so far.
        from ..models import RoutingEvent
        from ..router import INTAKE_REPLY, route_initial

        prior = (
            (await db.execute(select(Turn).where(Turn.session_id == session.id).order_by(Turn.turn_index)))
            .scalars()
            .all()
        )
        utterances = [t.user_message for t in prior if t.user_message] + [body.user_message]
        candidates = await _routing_candidates(db, scope)
        # never loop in intake: from the second non-routed turn on, force a choice
        decision = (
            await route_initial(candidates, utterances, force=len(prior) >= 1) if candidates else None
        )
        kind = decision.kind if decision else "oos"
        chosen = decision.sop_id if decision else None
        db.add(
            RoutingEvent(
                tenant_id=scope.tenant_id, project_id=scope.project_id, session_id=session.id,
                turn_index=len(prior), kind=kind, chosen_sop_id=chosen,
                reason=(decision.reason if decision else "no published SOPs"),
                router_ms=(decision.router_ms if decision else 0),
            )
        )
        if chosen:
            ver = next(c["version"] for c in candidates if c["id"] == chosen)
            await _assign_sop(db, scope, session, chosen, ver)
            await db.commit()
            routing_info = {"kind": "initial", "sop_id": chosen, "reason": decision.reason}
            log.info("routed session=%s -> sop=%s (%s)", session.id, chosen, decision.reason)
            # fall through to the normal flow: this same turn runs on the routed SOP
        else:
            reply = INTAKE_REPLY
            db.add(
                Turn(
                    session_id=session.id, turn_index=len(prior), user_message=body.user_message,
                    assistant_message=reply,
                    debug={"routing": {"kind": kind, "reason": decision.reason if decision else ""}},
                )
            )
            await db.commit()
            log.info("intake defer session=%s kind=%s", session.id, kind)
            return {
                "reply": reply, "terminal": None,
                "classification": {"state": "", "action": "", "cohort": "", "mood": ""},
                "turn": {"turn_index": len(prior), "chosen_action": "", "instruction_hit": False,
                          "picks": [], "consume_stats": {}, "rerank_ms": 0, "prompt_text": "", "context_block": ""},
                "routing": {"kind": kind, "sop_id": None,
                             "reason": decision.reason if decision else "no published SOPs"},
                "total_ms": int((_time.perf_counter() - t0) * 1000),
            }

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

    # D-12b: same-turn parallel prefetch — the fetch runs while we classify,
    # so even turn 0 pays max(classify, fetch), not the sum.
    if scope.retrieval_enabled or scope.sop_enabled:
        request.app.state.prefetch.prefetch_current_turn(
            scope=scope, session_id=session.id, task_def=task_def,
            user_text=body.user_message,
        )

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

    # D-11 switch check: only when the tracker lost the thread — the classified
    # state is outside (or absent from) the current SOP's vocabulary.
    state_vocab = {u.name for u in task_def.user_states}
    if not proposal["state"] or proposal["state"] not in state_vocab:
        from ..models import RoutingEvent
        from ..router import route_switch

        candidates = await _routing_candidates(db, scope)
        current_name = next((c["name"] for c in candidates if c["id"] == session.sop_id), "current")
        recent = [t.user_message for t in prior_turns[-1:] if t.user_message] + [body.user_message]
        decision = await route_switch(candidates, session.sop_id, current_name, recent)
        if decision.sop_id:
            db.add(
                RoutingEvent(
                    tenant_id=scope.tenant_id, project_id=scope.project_id, session_id=session.id,
                    turn_index=len(prior_turns), kind="switch", chosen_sop_id=decision.sop_id,
                    previous_sop_id=session.sop_id, reason=decision.reason, router_ms=decision.router_ms,
                )
            )
            ver = next(c["version"] for c in candidates if c["id"] == decision.sop_id)
            await _assign_sop(db, scope, session, decision.sop_id, ver)
            await db.commit()
            log.info("switched session=%s -> sop=%s (%s)", session.id, decision.sop_id, decision.reason)
            routing_info = {"kind": "switch", "sop_id": decision.sop_id, "reason": decision.reason}
            # re-plan this turn against the NEW SOP (fresh graph; prior actions
            # belong to the old procedure and don't constrain the new one)
            version = (
                await db.execute(
                    select(SopVersion).where(
                        SopVersion.sop_id == session.sop_id, SopVersion.version == session.sop_version
                    )
                )
            ).scalar_one()
            task_def = TaskDefinition.model_validate(version.definition)
            graph = SOPGraph(task_def)
            allowed = graph.allowed_actions(set())
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
        ),
        request,
        scope,
        db,
    )

    payload_for_agent = plan["prompt_text"] or plan["context_block"]
    t_respond = _time.perf_counter()
    reply = plan["prompt_text"] if plan["instruction_hit"] else await respond(
        payload_for_agent, history, body.user_message
    )
    respond_ms = int((_time.perf_counter() - t_respond) * 1000)
    turn_row = (
        await db.execute(
            select(Turn).where(Turn.session_id == session.id, Turn.turn_index == plan["turn_index"])
        )
    ).scalar_one_or_none()
    if turn_row is not None:
        turn_row.assistant_message = reply
        turn_row.duration_ms = int((_time.perf_counter() - t0) * 1000)
        turn_row.debug = {
            **(turn_row.debug or {}),
            "respond_ms": respond_ms,
            "reply_source": "pre-draft" if plan["instruction_hit"] else "model",
        }
    # Complete the precedent trace: the agent's actual reply (what instruction
    # pre-generation mines) and the situation embedding (already computed for
    # this turn's rerank — no extra API call).
    trace_row = (
        await db.execute(
            select(PrecedentTrace).where(
                PrecedentTrace.session_id == session.id,
                PrecedentTrace.turn_index == plan["turn_index"],
            )
        )
    ).scalar_one_or_none()
    if trace_row is not None:
        trace_row.response_text = reply
        if query_emb is not None:
            trace_row.situation_embedding = query_emb
    await db.commit()

    cp = task_def.conversation_profile
    terminal = (
        "success" if proposal["state"] in set(cp.success_markers)
        else "failure" if proposal["state"] in set(cp.failure_markers)
        else None
    )
    if terminal and session.terminal_outcome is None:
        # auto-record the detected terminal + back-propagate onto traces;
        # an explicit POST /outcome afterwards still overrides.
        session.terminal_outcome = terminal
        await db.execute(
            update(PrecedentTrace)
            .where(
                PrecedentTrace.tenant_id == scope.tenant_id,
                PrecedentTrace.session_id == session.id,
            )
            .values(terminal_outcome=terminal, terminal_reward=TERMINAL_REWARDS[terminal])
        )
        await db.commit()
    return {
        "reply": reply,
        "terminal": terminal,
        "classification": proposal,
        "turn": plan,
        "routing": routing_info,
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

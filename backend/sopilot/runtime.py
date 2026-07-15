"""Online-lane turn planning (the hot path). Pure assembly helpers live here so
they unit-test without a server; the router in api/runtime.py stays thin.

Mode gating (D-9):
  - sop_enabled:       stage-prompt + instruction assembly runs
  - retrieval_enabled: pool consume + context selection runs
Position tracking always runs — both subsystems key off it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .pool import PoolItem
from .rerank import RerankResult, speculative_context_block
from .schemas import TaskDefinition


@dataclass
class TurnPlan:
    turn_index: int
    chosen_action: str
    allowed_actions: list[str]
    subsystems: str
    prompt_text: str = ""              # sop mode: full per-turn instruction payload
    context_block: str = ""            # retrieval mode: the speculative block alone
    instruction_hit: bool = False      # sop+retrieval: pre-drafted instruction served verbatim
    picks: list[dict] = field(default_factory=list)
    consume_stats: dict = field(default_factory=dict)
    rerank_ms: int = 0


def choose_action(requested: str | None, allowed: list[str]) -> str:
    """P1 placeholder for the P2 classifier/proposer: honor an explicit request if
    it's legal, else take the first allowed action (deterministic)."""
    if requested and requested in allowed:
        return requested
    return allowed[0] if allowed else ""


def collect_prompt_block_names(task_def: TaskDefinition) -> set[str]:
    """All prompt-block names an SOP definition binds (publish-time existence check)."""
    names: set[str] = set()
    for action in task_def.agent_actions:
        names.update(action.prompt_blocks or [])
    return names


def assemble_stage_prompt(
    task_def: TaskDefinition,
    action_name: str,
    *,
    context_block: str = "",
    dep_payloads: dict[str, str] | None = None,
    instruction_text: str | None = None,
    stage_blocks: list[str] | None = None,
) -> str:
    """The per-turn instruction payload for the live agent.

    With a pre-drafted instruction (pool hit), the payload IS that text — served
    verbatim (D-5). Otherwise it assembles: role, goal, this stage's action +
    approved phrasing, resolved data, and the speculative context block.
    """
    if instruction_text is not None:
        return instruction_text

    cp = task_def.conversation_profile
    action = next((a for a in task_def.agent_actions if a.name == action_name), None)
    lines: list[str] = []
    if cp.agent_role:
        lines.append(f"ROLE: {cp.agent_role}")
    if cp.goal:
        lines.append(f"GOAL: {cp.goal}")
    if cp.knowledge:
        lines.append(f"BACKGROUND: {cp.knowledge}")
    for content in stage_blocks or []:  # D-7: authored, versioned language — pinned at session start
        lines.append(content)
    lines.append(f"CURRENT STAGE: {action_name}")
    if action is not None:
        if action.description:
            lines.append(f"STAGE INSTRUCTIONS: {action.description}")
        if action.must_say:
            lines.append("MUST INCLUDE: " + " | ".join(action.must_say))
        if action.must_not_say:
            lines.append("DO NOT SAY: " + " | ".join(action.must_not_say))
    if dep_payloads:
        lines.append("DATA FOR THIS STAGE:")
        for dep_name, payload in dep_payloads.items():
            lines.append(f"- {dep_name}: {payload}")
    if context_block:
        lines.append("")
        lines.append(context_block)
    return "\n".join(lines)


def build_plan(
    *,
    turn_index: int,
    subsystems: str,
    task_def: TaskDefinition,
    allowed_actions: list[str],
    chosen_action: str,
    picks: list[PoolItem] | None = None,
    rerank: RerankResult | None = None,
    dep_payloads: dict[str, str] | None = None,
    consume_stats: dict | None = None,
    instruction_item: PoolItem | None = None,
    stage_blocks: list[str] | None = None,
) -> TurnPlan:
    sop_on = subsystems in ("sop", "both")
    retrieval_on = subsystems in ("retrieval", "both")

    context_block = speculative_context_block(picks or []) if retrieval_on else ""
    prompt_text = ""
    instruction_hit = False
    if sop_on:
        if instruction_item is not None and retrieval_on:
            prompt_text = str(instruction_item.payload)
            instruction_hit = True
        else:
            prompt_text = assemble_stage_prompt(
                task_def,
                chosen_action,
                context_block=context_block,
                dep_payloads=dep_payloads,
                stage_blocks=stage_blocks,
            )
    return TurnPlan(
        turn_index=turn_index,
        chosen_action=chosen_action,
        allowed_actions=allowed_actions,
        subsystems=subsystems,
        prompt_text=prompt_text,
        context_block=context_block,
        instruction_hit=instruction_hit,
        picks=[
            {
                "item_id": p.item_id,
                "dependency_name": p.dependency_name,
                "payload_summary": p.payload_summary,
                "predictor_source": p.predictor_source,
            }
            for p in (picks or [])
        ],
        consume_stats=consume_stats or {},
        rerank_ms=rerank.duration_ms if rerank else 0,
    )

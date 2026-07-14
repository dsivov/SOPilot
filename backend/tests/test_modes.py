"""D-9 subsystem-mode gating: what plan-turn produces in each of the three modes."""
from datetime import timedelta

from sopilot.pool import PoolItem, utcnow
from sopilot.rerank import RerankResult
from sopilot.runtime import assemble_stage_prompt, build_plan, choose_action
from sopilot.schemas import ConversationProfile, NamedItem, TaskDefinition
from sopilot.tenancy import Scope


def make_task_def() -> TaskDefinition:
    return TaskDefinition(
        name="renewal",
        conversation_profile=ConversationProfile(
            agent_role="Insurance renewal agent", goal="Renew the policy", knowledge="ACME Insurance"
        ),
        agent_actions=[
            NamedItem(
                name="PitchRenewal",
                description="Present the renewal offer with real numbers.",
                must_say=["your current premium"],
                must_not_say=["guaranteed"],
            )
        ],
    )


def make_pick(summary: str = "policy #9 premium 480/yr") -> PoolItem:
    now = utcnow()
    return PoolItem(
        dependency_name="policy",
        source_action="PitchRenewal",
        payload=summary,
        payload_summary=summary,
        confidence=0.8,
        fetched_at=now,
        expires_at=now + timedelta(seconds=60),
    )


def test_scope_mode_flags():
    assert Scope("t", "p", "both").sop_enabled and Scope("t", "p", "both").retrieval_enabled
    assert Scope("t", "p", "sop").sop_enabled and not Scope("t", "p", "sop").retrieval_enabled
    assert not Scope("t", "p", "retrieval").sop_enabled and Scope("t", "p", "retrieval").retrieval_enabled


def test_choose_action():
    assert choose_action("B", ["A", "B"]) == "B"
    assert choose_action("Z", ["A", "B"]) == "A"  # illegal request → deterministic first-allowed
    assert choose_action(None, ["A", "B"]) == "A"
    assert choose_action(None, []) == ""


def test_both_mode_full_payload():
    plan = build_plan(
        turn_index=0, subsystems="both", task_def=make_task_def(),
        allowed_actions=["PitchRenewal"], chosen_action="PitchRenewal",
        picks=[make_pick()], rerank=RerankResult([make_pick()], "r", 12),
        dep_payloads={"policy": "policy #9"},
    )
    assert "ROLE: Insurance renewal agent" in plan.prompt_text
    assert "MUST INCLUDE: your current premium" in plan.prompt_text
    assert "DO NOT SAY: guaranteed" in plan.prompt_text
    assert "SPECULATIVELY PRE-STAGED" in plan.prompt_text  # context block embedded
    assert plan.context_block  # also exposed separately
    assert plan.rerank_ms == 12


def test_sop_only_mode_no_speculation():
    plan = build_plan(
        turn_index=0, subsystems="sop", task_def=make_task_def(),
        allowed_actions=["PitchRenewal"], chosen_action="PitchRenewal",
        picks=[make_pick()],  # even if picks leak in, sop mode must not use them
        dep_payloads={"policy": "policy #9 (fetched live)"},
    )
    assert "ROLE:" in plan.prompt_text
    assert "DATA FOR THIS STAGE" in plan.prompt_text  # live-resolved data present
    assert "SPECULATIVELY PRE-STAGED" not in plan.prompt_text
    assert plan.context_block == ""


def test_retrieval_only_mode_context_block_only():
    plan = build_plan(
        turn_index=0, subsystems="retrieval", task_def=make_task_def(),
        allowed_actions=["PitchRenewal"], chosen_action="PitchRenewal",
        picks=[make_pick()], rerank=RerankResult([make_pick()], "r", 9),
    )
    assert plan.prompt_text == ""  # customer owns their own prompting
    assert "SPECULATIVELY PRE-STAGED" in plan.context_block
    assert plan.picks[0]["dependency_name"] == "policy"


def test_instruction_hit_served_verbatim_only_with_both():
    instr = make_pick("pre-drafted pitch")
    instr.kind = "instruction"
    instr.payload = "Hello! Your renewal is ready at 480/yr — shall we proceed?"
    plan = build_plan(
        turn_index=0, subsystems="both", task_def=make_task_def(),
        allowed_actions=["PitchRenewal"], chosen_action="PitchRenewal",
        instruction_item=instr,
    )
    assert plan.instruction_hit is True
    assert plan.prompt_text == instr.payload  # verbatim (D-5)
    # sop-only: no pool → instruction ignored even if passed
    plan2 = build_plan(
        turn_index=0, subsystems="sop", task_def=make_task_def(),
        allowed_actions=["PitchRenewal"], chosen_action="PitchRenewal",
        instruction_item=instr,
    )
    assert plan2.instruction_hit is False
    assert "ROLE:" in plan2.prompt_text


def test_assemble_prompt_without_optional_fields():
    text = assemble_stage_prompt(TaskDefinition(name="x", agent_actions=[NamedItem(name="A")]), "A")
    assert "CURRENT STAGE: A" in text

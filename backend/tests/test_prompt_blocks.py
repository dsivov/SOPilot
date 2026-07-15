"""D-7 prompt-block behavior: assembly ordering, binding collection, mode interplay."""
from sopilot.runtime import assemble_stage_prompt, build_plan, collect_prompt_block_names
from sopilot.schemas import ConversationProfile, NamedItem, TaskDefinition

COMPLIANCE = "COMPLIANCE: Recording disclosure — this call may be recorded for quality purposes."
STAGE_BLOCK = "OFFER RULES: quote only the premium from the policy data; never invent discounts."


def make_task_def() -> TaskDefinition:
    return TaskDefinition(
        name="renewal",
        conversation_profile=ConversationProfile(agent_role="Renewal agent", goal="Renew"),
        agent_actions=[
            NamedItem(name="Greeting", prompt_blocks=["compliance.recording"]),
            NamedItem(
                name="PitchRenewal",
                description="Present the offer.",
                must_say=["your current premium"],
                prompt_blocks=["compliance.recording", "stage.pitch_rules"],
            ),
        ],
    )


def test_collect_prompt_block_names():
    assert collect_prompt_block_names(make_task_def()) == {"compliance.recording", "stage.pitch_rules"}
    assert collect_prompt_block_names(TaskDefinition(name="x")) == set()


def test_blocks_injected_between_header_and_stage():
    text = assemble_stage_prompt(
        make_task_def(), "PitchRenewal", stage_blocks=[COMPLIANCE, STAGE_BLOCK]
    )
    # order: role header → authored blocks → stage line → constraints
    assert text.index("ROLE:") < text.index("COMPLIANCE:") < text.index("CURRENT STAGE:")
    assert text.index("COMPLIANCE:") < text.index("OFFER RULES:")
    assert "MUST INCLUDE: your current premium" in text


def test_no_blocks_no_change():
    with_none = assemble_stage_prompt(make_task_def(), "PitchRenewal")
    with_empty = assemble_stage_prompt(make_task_def(), "PitchRenewal", stage_blocks=[])
    assert with_none == with_empty
    assert "COMPLIANCE" not in with_none


def test_build_plan_passes_blocks_only_in_sop_modes():
    plan = build_plan(
        turn_index=0, subsystems="both", task_def=make_task_def(),
        allowed_actions=["PitchRenewal"], chosen_action="PitchRenewal",
        stage_blocks=[COMPLIANCE],
    )
    assert "COMPLIANCE:" in plan.prompt_text
    plan_r = build_plan(
        turn_index=0, subsystems="retrieval", task_def=make_task_def(),
        allowed_actions=["PitchRenewal"], chosen_action="PitchRenewal",
        stage_blocks=[COMPLIANCE],
    )
    assert plan_r.prompt_text == ""  # retrieval-only mode: customer owns prompting


def test_verbatim_instruction_hit_bypasses_blocks():
    """On an instruction hit the payload is served verbatim (D-5) — blocks were
    already baked in at pre-generation time, not re-appended."""
    from datetime import timedelta

    from sopilot.pool import PoolItem, utcnow

    now = utcnow()
    instr = PoolItem(
        dependency_name="instruction:PitchRenewal", source_action="PitchRenewal",
        payload="Pre-drafted, compliance-approved pitch.", payload_summary="pitch",
        confidence=0.9, fetched_at=now, expires_at=now + timedelta(seconds=60),
        kind="instruction",
    )
    plan = build_plan(
        turn_index=0, subsystems="both", task_def=make_task_def(),
        allowed_actions=["PitchRenewal"], chosen_action="PitchRenewal",
        instruction_item=instr, stage_blocks=[COMPLIANCE],
    )
    assert plan.prompt_text == "Pre-drafted, compliance-approved pitch."

from sopilot.schemas import ConversationProfile, NamedItem, SOPEdge, SOPGraphSchema, TaskDefinition
from sopilot.sop_graph import SOPGraph


def make_task() -> TaskDefinition:
    return TaskDefinition(
        name="t",
        agent_actions=[
            NamedItem(name="Greeting"),
            NamedItem(name="VerifyIdentity"),
            NamedItem(name="PitchRenewal"),
            NamedItem(name="HandleObjection"),
            NamedItem(name="ClosePolite"),
        ],
        user_states=[
            NamedItem(name="Objecting"),
            NamedItem(name="HardDecline"),
            NamedItem(name="AgreedToRenew"),
        ],
        conversation_profile=ConversationProfile(
            success_markers=["AgreedToRenew"], failure_markers=["HardDecline"]
        ),
        sop=SOPGraphSchema(
            edges=[
                SOPEdge(src="Greeting", dst="VerifyIdentity"),
                SOPEdge(src="VerifyIdentity", dst="PitchRenewal"),
                SOPEdge(src="Objecting", dst="HandleObjection"),  # state trigger
                SOPEdge(src="HardDecline", dst="ClosePolite"),  # state trigger
            ]
        ),
    )


def test_ordering_prereqs():
    g = SOPGraph(make_task())
    assert g.allowed_actions(set()) == ["Greeting"]
    assert "VerifyIdentity" in g.allowed_actions({"Greeting"})
    assert "PitchRenewal" not in g.allowed_actions({"Greeting"})
    assert "PitchRenewal" in g.allowed_actions({"Greeting", "VerifyIdentity"})


def test_state_triggered_actions_gated_until_trigger():
    g = SOPGraph(make_task())
    # ClosePolite must not fire at turn 1 (no HardDecline yet)
    assert "ClosePolite" not in g.allowed_actions({"Greeting"})
    assert "ClosePolite" in g.allowed_actions({"Greeting", "HardDecline"})
    assert "HandleObjection" in g.allowed_actions({"Greeting", "Objecting"})


def test_missed_state_never_blocks_ordering_chain():
    """The credit-card fix: an unvisited state prereq must not deadlock actions
    whose ordering prereqs are met."""
    g = SOPGraph(make_task())
    allowed = g.allowed_actions({"Greeting", "VerifyIdentity"})
    assert "PitchRenewal" in allowed  # no state gate on the ordering chain


def test_lint_clean_sop_passes():
    assert SOPGraph(make_task()).lint() == []


def test_lint_catches_cycle_and_unknown_nodes():
    task = make_task()
    task.sop.edges.append(SOPEdge(src="PitchRenewal", dst="Greeting"))  # cycle
    task.sop.edges.append(SOPEdge(src="Ghost", dst="Greeting"))  # unknown node
    problems = SOPGraph(task).lint()
    assert any("cycle" in p for p in problems)
    assert any("unknown node 'Ghost'" in p for p in problems)


def test_lint_catches_bad_marker_and_missing_dep():
    task = make_task()
    task.conversation_profile.success_markers.append("NotAState")
    task.agent_actions[2].data_dependencies.append("missing_dep")
    problems = SOPGraph(task).lint()
    assert any("NotAState" in p for p in problems)
    assert any("missing_dep" in p for p in problems)


def test_fallback_to_full_catalog_when_stuck():
    task = make_task()
    # Make everything gated behind an unreachable trigger except nothing
    task.sop.edges = [SOPEdge(src="AgreedToRenew", dst=a.name) for a in task.agent_actions]
    g = SOPGraph(task)
    assert g.allowed_actions(set()) == sorted(g.action_names)

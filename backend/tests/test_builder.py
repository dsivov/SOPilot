"""merge_patch / derive_nodes — the Studio's structural guarantees (no LLM here)."""
from sopilot.builder import derive_nodes, merge_patch
from sopilot.schemas import TaskDefinition


def base() -> dict:
    return {
        "name": "renewal",
        "agent_actions": [
            {"name": "Greeting", "description": "open the call"},
            {"name": "VerifyIdentity"},
        ],
        "user_states": [{"name": "Objecting"}],
        "sop": {"edges": [{"src": "Greeting", "dst": "VerifyIdentity", "direction": "forward"}]},
    }


def test_scalar_replace_and_profile_shallow_merge():
    out = merge_patch(
        {"name": "a", "conversation_profile": {"agent_role": "r", "goal": "g"}},
        {"name": "b", "conversation_profile": {"goal": "g2"}},
    )
    assert out["name"] == "b"
    assert out["conversation_profile"] == {"agent_role": "r", "goal": "g2"}


def test_named_list_merge_updates_and_adds():
    out = merge_patch(base(), {
        "agent_actions": [
            {"name": "Greeting", "must_say": ["hello"]},          # update in place
            "PitchRenewal",                                        # bare string normalized
        ]
    })
    by_name = {a["name"]: a for a in out["agent_actions"]}
    assert by_name["Greeting"]["description"] == "open the call"  # untouched field survives
    assert by_name["Greeting"]["must_say"] == ["hello"]
    assert "PitchRenewal" in by_name and "VerifyIdentity" in by_name


def test_named_list_delete():
    out = merge_patch(base(), {"agent_actions": [{"name": "VerifyIdentity", "_delete": True}]})
    assert [a["name"] for a in out["agent_actions"]] == ["Greeting"]
    assert "_delete" not in str(out)


def test_edges_dedupe_and_accumulate():
    out = merge_patch(base(), {"sop": {"edges": [
        {"src": "Greeting", "dst": "VerifyIdentity", "direction": "forward"},  # dup → ignored
        {"src": "Objecting", "dst": "VerifyIdentity", "direction": "forward"},
    ]}})
    assert len(out["sop"]["edges"]) == 2


def test_nodes_always_rederived_including_edge_endpoints():
    patched = merge_patch(base(), {"sop": {"edges": [{"src": "GhostState", "dst": "Greeting"}]}})
    nodes = patched["sop"]["nodes"]
    assert {"Greeting", "VerifyIdentity", "Objecting", "GhostState"} <= set(nodes)
    # the ghost node survives merge so the LINTER reports it (not silently dropped)
    assert derive_nodes(patched) == nodes


def test_merge_result_is_schema_valid():
    out = merge_patch(base(), {
        "data_dependencies": [{"name": "policy", "kind": "mock", "idempotent": True}],
        "agent_actions": [{"name": "PitchRenewal", "data_dependencies": ["policy"]}],
    })
    task_def = TaskDefinition.model_validate(out)
    assert task_def.data_dependencies[0].name == "policy"


def test_inline_dep_objects_hoisted_to_catalog():
    """LLMs sometimes inline full dependency objects on actions — normalize, don't reject."""
    out = merge_patch({}, {
        "name": "x",
        "agent_actions": [{
            "name": "StateBalance",
            "data_dependencies": [
                {"name": "BillingSystem", "kind": "db", "description": "balance lookup"},
                "existing_ref",
            ],
        }],
    })
    action = out["agent_actions"][0]
    assert action["data_dependencies"] == ["BillingSystem", "existing_ref"]
    catalog = {d["name"]: d for d in out["data_dependencies"]}
    assert catalog["BillingSystem"]["kind"] == "db"
    TaskDefinition.model_validate(out)


def test_extract_document_text_pdf_and_plain():
    from pathlib import Path

    from sopilot.builder import extract_document_text

    pdf = Path(__file__).resolve().parents[2] / "docs" / "examples" / "appointment_scheduling_sop.pdf"
    text = extract_document_text("appointment_scheduling_sop.pdf", pdf.read_bytes())
    assert "Verify the patient's identity" in text
    assert "cancellations are released at 8am" in text  # mandated wording survives extraction
    assert extract_document_text("notes.txt", "plain body".encode()) == "plain body"


def test_empty_patch_is_identity_plus_nodes():
    out = merge_patch(base(), {})
    assert out["name"] == "renewal"
    assert set(out["sop"]["nodes"]) == {"Greeting", "VerifyIdentity", "Objecting"}

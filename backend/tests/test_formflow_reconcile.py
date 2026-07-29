"""Backward edit-propagation — void answers a later edit has hidden (to a fixpoint)."""
from sopilot.api.formflow import _reconcile_stale, _is_real_answer

MANIFEST = {
    "order": ["s0"],
    "stages": {
        "s0": {"title": "Intake", "block": "b0", "fields": [
            {"name": "admitted", "id": 1, "label": "Were you admitted?", "cond": ""},
            {"name": "how_long", "id": 2, "label": "How long?", "cond": "isYes({admitted})"},
            {"name": "ward", "id": 3, "label": "Which ward?", "cond": "isYes({how_long})"},   # cascades off how_long
            {"name": "age", "id": 4, "label": "Age", "cond": ""},
            {"name": "guardian", "id": 5, "label": "Guardian name", "cond": "values[{age}] < 18"},
        ]},
    },
}


def test_no_stale_when_all_visible():
    ans = {"admitted": "Yes", "how_long": "5 days", "age": "40"}
    voided, _ = _reconcile_stale(MANIFEST, ans)
    assert voided == []


def test_voids_answered_then_hidden():
    # admitted flipped to No → how_long (answered "5 days") is now hidden → void it
    ans = {"admitted": "No", "how_long": "5 days", "age": "40"}
    voided, out = _reconcile_stale(MANIFEST, ans)
    assert {v["name"] for v in voided} == {"how_long"}
    assert out["how_long"] == "not applicable"


def test_cascade_to_fixpoint():
    # admitted=No hides how_long; voiding how_long hides ward → both voided in one call
    ans = {"admitted": "No", "how_long": "Yes", "ward": "ICU", "age": "40"}
    voided, out = _reconcile_stale(MANIFEST, ans)
    assert {v["name"] for v in voided} == {"how_long", "ward"}
    assert out["ward"] == "not applicable"


def test_numeric_gate_reopen_is_not_voided():
    # age=10 → guardian visible; a real guardian answer must be KEPT
    ans = {"admitted": "No", "age": "10", "guardian": "Jane Roe"}
    voided, _ = _reconcile_stale(MANIFEST, ans)
    assert all(v["name"] != "guardian" for v in voided)


def test_sentinel_answers_are_not_real():
    assert not _is_real_answer("not applicable")
    assert not _is_real_answer("")
    assert not _is_real_answer(None)
    assert _is_real_answer("5 days")


def test_already_voided_field_is_idempotent():
    ans = {"admitted": "No", "how_long": "not applicable", "age": "40"}
    voided, _ = _reconcile_stale(MANIFEST, ans)
    assert voided == []   # already "not applicable" → nothing to do

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


# --- repeaters: a group (med_name, med_prescribed, med_rx_num) + the "add another" repeater ---
REP = {
    "order": ["s0"],
    "stages": {"s0": {"title": "Meds", "block": "b", "fields": [
        {"name": "takes_meds", "id": 1, "label": "Do you take medications?", "cond": ""},
        {"name": "med_name", "id": 2, "label": "Medication", "cond": "isYes({takes_meds})", "repeat_group": "add_med"},
        {"name": "med_prescribed", "id": 3, "label": "Prescribed?", "cond": "isYes({takes_meds})", "repeat_group": "add_med"},
        {"name": "med_rx_num", "id": 4, "label": "Rx number", "cond": "isYes({med_prescribed})", "repeat_group": "add_med"},
        {"name": "add_med", "id": 5, "label": "Add another medication?", "cond": "isYes({takes_meds})",
         "repeater": True, "members": ["med_name", "med_prescribed", "med_rx_num"]},
    ]}},
}


def test_repeater_per_instance_void():
    # instance 0: prescribed=Yes → rx kept; instance 1: prescribed=No → rx_num[1] is stale
    ans = {"takes_meds": "Yes",
           "med_name[0]": "Aspirin", "med_prescribed[0]": "Yes", "med_rx_num[0]": "R123",
           "med_name[1]": "Advil", "med_prescribed[1]": "No", "med_rx_num[1]": "R999"}
    voided, out = _reconcile_stale(REP, ans)
    assert {v["name"] for v in voided} == {"med_rx_num[1]"}
    assert out["med_rx_num[1]"] == "not applicable"
    assert out["med_rx_num[0]"] == "R123"   # instance 0 untouched


def test_repeater_whole_group_hidden_voids_all_instances():
    # takes_meds flipped to No → the whole repeater is hidden → every instance answer voided
    ans = {"takes_meds": "No",
           "med_name[0]": "Aspirin", "med_prescribed[0]": "Yes", "med_rx_num[0]": "R123",
           "med_name[1]": "Advil", "med_prescribed[1]": "No",
           "add_med": "No"}
    voided, out = _reconcile_stale(REP, ans)
    # every instance answer AND the now-moot "add another?" control itself
    assert {v["name"] for v in voided} == {"med_name[0]", "med_prescribed[0]", "med_rx_num[0]",
                                           "med_name[1]", "med_prescribed[1]", "add_med"}
    assert all(out[k] == "not applicable" for k in
               ("med_name[0]", "med_prescribed[0]", "med_rx_num[0]", "med_name[1]", "med_prescribed[1]"))


def test_repeater_no_stale_when_consistent():
    ans = {"takes_meds": "Yes",
           "med_name[0]": "Aspirin", "med_prescribed[0]": "Yes", "med_rx_num[0]": "R123",
           "med_name[1]": "Advil", "med_prescribed[1]": "No"}   # rx[1] correctly absent
    voided, _ = _reconcile_stale(REP, ans)
    assert voided == []

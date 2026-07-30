"""Prepare over repeating groups — serve each instance's members + the 'add another?' control."""
from sopilot.api.formflow import _visible_unanswered

REP = {
    "order": ["s0"],
    "stages": {"s0": {"title": "Meds", "block": "b", "fields": [
        {"name": "takes_meds", "id": 1, "label": "Do you take medications?", "cond": ""},
        {"name": "med_name", "id": 2, "label": "Medication", "cond": "isYes({takes_meds})", "repeat_group": "add_med"},
        {"name": "med_prescribed", "id": 3, "label": "Prescribed?", "cond": "isYes({takes_meds})", "repeat_group": "add_med"},
        {"name": "med_rx_num", "id": 4, "label": "Rx number", "cond": "isYes({med_prescribed})", "repeat_group": "add_med"},
        {"name": "add_med", "id": 5, "label": "Add another medication?", "cond": "isYes({takes_meds})",
         "repeater": True, "members": ["med_name", "med_prescribed", "med_rx_num"]},
        {"name": "done_note", "id": 6, "label": "Anything else?", "cond": ""},
    ]}},
}


def nxt(ans, cursor=None):
    _, f = _visible_unanswered(REP, ans, None, cursor)
    return f["name"] if f else None


def test_group_skipped_when_repeater_hidden():
    # takes_meds unanswered → ask it first; then No → whole group hidden → jump past to done_note
    assert nxt({}) == "takes_meds"
    assert nxt({"takes_meds": "No"}) == "done_note"


def test_serves_first_instance_members_in_order():
    a = {"takes_meds": "Yes"}
    assert nxt(a) == "med_name[0]"
    a["med_name[0]"] = "Aspirin"
    assert nxt(a) == "med_prescribed[0]"


def test_per_instance_gating_inside_group():
    a = {"takes_meds": "Yes", "med_name[0]": "Aspirin", "med_prescribed[0]": "Yes"}
    assert nxt(a) == "med_rx_num[0]"                 # prescribed=Yes → rx asked
    a2 = {"takes_meds": "Yes", "med_name[0]": "Aspirin", "med_prescribed[0]": "No"}
    assert nxt(a2) == "add_med[0]"                    # prescribed=No → rx skipped → 'add another?'


def test_add_another_opens_next_instance():
    a = {"takes_meds": "Yes", "med_name[0]": "Aspirin", "med_prescribed[0]": "No", "add_med[0]": "Yes"}
    assert nxt(a) == "med_name[1]"                    # Yes → instance 1


def test_no_more_ends_group_and_moves_on():
    a = {"takes_meds": "Yes", "med_name[0]": "Aspirin", "med_prescribed[0]": "No", "add_med[0]": "No"}
    assert nxt(a) == "done_note"                      # No → past the group


def test_completes_when_all_done():
    a = {"takes_meds": "Yes", "med_name[0]": "Aspirin", "med_prescribed[0]": "No",
         "add_med[0]": "No", "done_note": "nope"}
    assert nxt(a) is None

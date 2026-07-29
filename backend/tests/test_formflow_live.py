"""Live pt-forms wiring — id↔FieldName bridge for /formflow/prepare.

get-fields addresses fields by numeric id and strips FieldName; the __map__
block carries the id↔name bridge. These cover the conversion the live path does
before handing off to the (already-tested) deterministic driver.
"""
from sopilot.api.formflow import _id_to_name, _live_answers, _normalize_field, _visible_unanswered

# Mirrors the pipeline's __map__ output: each field carries its pt-forms get-fields id.
MANIFEST = {
    "order": ["s0", "s1"],
    "stages": {
        "s0": {"title": "Intake", "block": "b0", "fields": [
            {"name": "age", "id": 2, "label": "Age", "cond": ""},
            {"name": "kids", "id": 3, "label": "How many children?", "cond": "values[{age}] >= 18"},
            {"name": "hosp", "id": 4, "label": "Went to hospital?", "cond": ""},
            {"name": "hosp_name", "id": 5, "label": "Hospital name", "cond": "isYes({hosp})"},
        ]},
        "s1": {"title": "Legal", "block": "b1", "fields": [
            {"name": "atty", "id": 7, "label": "Have an attorney?", "cond": ""},
            {"name": "atty_name", "id": 8, "label": "Attorney name", "cond": "isYes({atty})"},
        ]},
    },
}


def test_id_to_name_indexes_every_field():
    assert _id_to_name(MANIFEST) == {2: "age", 3: "kids", 4: "hosp", 5: "hosp_name", 7: "atty", 8: "atty_name"}


def test_live_answers_maps_id_keyed_values_to_fieldnames():
    body = {"values": {"2": "40", "4": "No"}}   # get-fields is id-keyed
    assert _live_answers(body, MANIFEST) == {"age": "40", "hosp": "No"}


def test_live_answers_data_alias_and_repeater_suffix():
    # some responses use `data`; repeater instances arrive as "<id>[i]" → base field
    body = {"data": {"7": "Yes", "8[0]": "Jane Roe"}}
    assert _live_answers(body, MANIFEST) == {"atty": "Yes", "atty_name": "Jane Roe"}


def test_live_answers_unknown_id_passes_through():
    body = {"values": {"999": "x"}}
    assert _live_answers(body, MANIFEST) == {"999": "x"}


def test_normalize_field_accepts_id_or_name_or_none():
    assert _normalize_field(MANIFEST, "4") == "hosp"        # numeric id → FieldName
    assert _normalize_field(MANIFEST, "4[0]") == "hosp"     # repeater id → base FieldName
    assert _normalize_field(MANIFEST, "hosp") == "hosp"     # already a FieldName
    assert _normalize_field(MANIFEST, None) is None


def test_live_snapshot_drives_gating_end_to_end():
    # get-fields snapshot: age=40 (kids opens), hosp=No (hosp_name skipped).
    # cursor at id 4 (hosp) → next visible unanswered crosses into s1 (atty).
    body = {"values": {"2": "40", "3": "2", "4": "No"}}
    answers = _live_answers(body, MANIFEST)
    cur = _normalize_field(MANIFEST, "4")
    sid, field = _visible_unanswered(MANIFEST, answers, "s0", cur)
    assert sid == "s1" and field["name"] == "atty" and field["id"] == 7


def test_live_snapshot_gate_opens_hospital_name():
    body = {"values": {"2": "40", "3": "2", "4": "Yes"}}   # hosp=Yes → hosp_name visible
    answers = _live_answers(body, MANIFEST)
    sid, field = _visible_unanswered(MANIFEST, answers, "s0", _normalize_field(MANIFEST, "4"))
    assert field["name"] == "hosp_name" and field["id"] == 5

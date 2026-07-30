"""Pipeline emits repeater metadata (Repeater + RepeatFrom → members / repeat_group)."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
import form_to_sop as f2s  # noqa: E402

FIXTURE = {"fields": [
    {"FieldName": "S1", "FieldType": "Section", "FieldValue": "Medications"},
    {"FieldName": "TakesMeds", "FieldType": "YesNo", "FieldNameAlt": "Do you take medications?"},
    {"FieldName": "MedName", "FieldType": "Text", "FieldNameAlt": "Medication", "FieldCondition": "isYes({TakesMeds})"},
    {"FieldName": "MedDose", "FieldType": "Text", "FieldNameAlt": "Dose", "FieldCondition": "isYes({TakesMeds})"},
    {"FieldName": "AddMed", "FieldType": "Repeater", "FieldNameAlt": "Add another?", "RepeatFrom": "MedName",
     "FieldCondition": "isYes({TakesMeds})"},
    {"FieldName": "DoneNote", "FieldType": "Text", "FieldNameAlt": "Anything else?"},
]}


def _parse():
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(FIXTURE, fh)
        path = fh.name
    try:
        return f2s.parse(path)
    finally:
        os.unlink(path)


def test_repeater_and_members_marked():
    _, byname = _parse()
    assert byname["AddMed"].get("repeater") is True
    assert byname["AddMed"]["members"] == ["MedName", "MedDose"]
    assert byname["MedName"]["repeat_group"] == "AddMed"
    assert byname["MedDose"]["repeat_group"] == "AddMed"


def test_non_members_untouched():
    _, byname = _parse()
    assert "repeat_group" not in byname["TakesMeds"]
    assert "repeat_group" not in byname["DoneNote"]


def test_ids_match_ptforms_indexing():
    # 1-based over every FieldName entry incl. the Section
    _, byname = _parse()
    assert byname["TakesMeds"]["id"] == 2
    assert byname["AddMed"]["id"] == 5


def test_group_stays_in_one_stage():
    sections, _ = _parse()
    stages = f2s.build_stages(sections)
    # the whole repeat group + repeater land in a single stage
    for st in stages:
        names = {f["name"] for f in st["fields"]}
        if "AddMed" in names:
            assert {"MedName", "MedDose"} <= names

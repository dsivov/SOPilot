"""Form-flow driver — deterministic stage resolution + gated next-field (no LLM)."""
from sopilot.api.formflow import _stage_of, _visible_unanswered

MANIFEST = {
    "order": ["s0", "s1"],
    "stages": {
        "s0": {"title": "Intake", "block": "b0", "fields": [
            {"name": "age", "label": "Age", "cond": ""},
            {"name": "kids", "label": "How many children?", "cond": "values[{age}] >= 18"},
            {"name": "hosp", "label": "Went to hospital?", "cond": ""},
            {"name": "hosp_name", "label": "Hospital name", "cond": "isYes({hosp})"},
        ]},
        "s1": {"title": "Legal", "block": "b1", "fields": [
            {"name": "atty", "label": "Have an attorney?", "cond": ""},
            {"name": "atty_name", "label": "Attorney name", "cond": "isYes({atty})"},
        ]},
    },
}


def test_stage_of():
    assert _stage_of(MANIFEST, "hosp") == "s0"
    assert _stage_of(MANIFEST, "atty") == "s1"
    assert _stage_of(MANIFEST, None) is None


def test_start_returns_first_field():
    sid, f = _visible_unanswered(MANIFEST, {}, None, None)
    assert sid == "s0" and f["name"] == "age"


def test_numeric_gate_skips_when_false():
    # age=4 → kids hidden; from after age, next visible unanswered is hosp
    sid, f = _visible_unanswered(MANIFEST, {"age": "4"}, "s0", "age")
    assert f["name"] == "hosp"


def test_numeric_gate_shows_when_true():
    sid, f = _visible_unanswered(MANIFEST, {"age": "40"}, "s0", "age")
    assert f["name"] == "kids"


def test_yesno_gate_and_cross_stage_transition():
    # hosp=No → hosp_name skipped; s0 exhausted → cross into s1 (atty)
    ans = {"age": "40", "kids": "2", "hosp": "No"}
    sid, f = _visible_unanswered(MANIFEST, ans, "s0", "hosp")
    assert sid == "s1" and f["name"] == "atty"


def test_yesno_gate_open():
    ans = {"age": "40", "kids": "2", "hosp": "Yes"}
    sid, f = _visible_unanswered(MANIFEST, ans, "s0", "hosp")
    assert f["name"] == "hosp_name"


def test_complete_when_all_visible_answered():
    ans = {"age": "4", "hosp": "No", "atty": "No"}   # kids/hosp_name/atty_name all gated off
    sid, f = _visible_unanswered(MANIFEST, ans, None, None)
    assert sid is None and f is None

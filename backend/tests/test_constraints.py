"""Canonical constraint engine tests — conditions (#1), value predicates (#2),
server-side evaluation (#3), order/sections (#4)."""
from sopilot.constraints import (
    State, evaluate_condition, evaluate_rules, predicate_holds, rule_findings,
    visible_fields,
)


# ---- conditions (#1) + value comparisons (#2) ----

def test_condition_isyes_isno():
    assert evaluate_condition("isYes({attorney})", {"attorney": "Yes"}) is True
    assert evaluate_condition("isYes({attorney})", {"attorney": "No"}) is False
    assert evaluate_condition("isNo({attorney})", {"attorney": "No"}) is True


def test_condition_value_comparisons():
    assert evaluate_condition("values[{age}] >= 18", {"age": "40"}) is True
    assert evaluate_condition("values[{age}] >= 18", {"age": "4"}) is False
    assert evaluate_condition("values[{onset}] == 'Gradually'", {"onset": "Gradually"}) is True
    assert evaluate_condition("values[{onset}] == 'Gradually'", {"onset": "Suddenly"}) is False


def test_condition_boolean_combinations_and_missing():
    assert evaluate_condition("isYes({a}) and values[{n}] > 0", {"a": "Yes", "n": "3"}) is True
    assert evaluate_condition("isYes({a}) and values[{n}] > 0", {"a": "Yes", "n": "0"}) is False
    assert evaluate_condition("isYes({a}) or isYes({b})", {"b": "Yes"}) is True
    assert evaluate_condition("values[{missing}] > 0", {}) is False   # unanswered → not visible
    assert evaluate_condition(None, {}) is True                       # no condition → visible


def test_condition_rejects_unsafe():
    assert evaluate_condition("__import__('os').system('x')", {}) is False


# ---- rule predicates: set-ness + value predicates (#2) ----

def test_predicate_setness_and_tools():
    st = State(values={"notify_url": "https://x"}, tools={"send_email"})
    assert predicate_holds("field:notify_url", st) is True
    assert predicate_holds("field:missing", st) is False
    assert predicate_holds("tool:send_email", st) is True
    assert predicate_holds("tool:hangup|send_email", st) is True


def test_predicate_value_comparisons():
    st = State(values={"channel": "email", "age": "40"})
    assert predicate_holds("field:channel == 'email'", st) is True
    assert predicate_holds("field:channel == 'sms'", st) is False
    assert predicate_holds("field:age >= 18", st) is True
    assert predicate_holds("field:age >= 65", st) is False


# ---- visible_fields: order + condition + sections (#1, #4) ----

SCHEMA = {"fields": [
    {"path": "sec1", "type": "section", "label": "About you"},
    {"path": "age", "type": "number", "label": "Age"},
    {"path": "children", "type": "number", "label": "How many children?",
     "condition": "values[{age}] >= 18"},
    {"path": "attorney", "type": "enum", "options": ["Yes", "No"], "label": "Have an attorney?"},
    {"path": "attorney_name", "type": "string", "label": "Attorney's name",
     "condition": "isYes({attorney})"},
]}


def test_visible_fields_honours_conditions_and_sections():
    paths = lambda ans: [f.path for f in visible_fields(SCHEMA, ans)]
    assert "children" not in paths({"age": "4"})
    assert "children" in paths({"age": "40"})
    assert "attorney_name" not in paths({"attorney": "No"})
    assert "attorney_name" in paths({"attorney": "Yes"})
    # section carried onto following fields
    vf = {f.path: f for f in visible_fields(SCHEMA, {"age": "40"})}
    assert vf["age"].section == "About you" and vf["sec1"].is_section


# ---- rule evaluation (#3), value-aware ----

def test_rules_requires_with_value_predicate():
    rules = [{"kind": "requires", "when": "field:channel == 'email'",
              "needs": "field:notify_url", "level": "error", "msg": "email needs a notify url"}]
    # when false → inactive
    assert evaluate_rules(rules, State(values={"channel": "sms"}))[0].state == "inactive"
    # when true, needs missing → violated
    assert evaluate_rules(rules, State(values={"channel": "email"}))[0].state == "violated"
    # when true, needs present → satisfied
    st = State(values={"channel": "email", "notify_url": "https://x"})
    assert evaluate_rules(rules, st)[0].state == "satisfied"


def test_rules_enum_and_conflicts_and_findings():
    rules = [
        {"kind": "enum", "field": "voice", "options": ["alloy", "echo"], "level": "error", "msg": "bad voice"},
        {"kind": "conflicts", "a": "tool:a", "b": "tool:b", "level": "warn", "msg": "a conflicts b"},
    ]
    assert evaluate_rules(rules, State(values={"voice": "banana"}))[0].state == "violated"
    assert evaluate_rules(rules, State(values={"voice": "echo"}))[0].state == "satisfied"
    both = State(tools={"a", "b"})
    fs = rule_findings(rules, both)
    assert any(f["msg"] == "a conflicts b" and f["level"] == "warn" for f in fs)

"""Form-navigation pilot — resolver + eligibility tests (PolarTie SmartForm #1)."""
import json
import os

import pytest

from sopilot.formnav import FormGraph, resolve_navigation

# A small synthetic form exercising: sections, a Yes/No gate, a numeric gate,
# a choice-value gate, and a repeater "add another?".
SYNTHETIC = [
    {"FieldName": "sec1", "FieldType": "Section", "FieldValue": "About you"},
    {"FieldName": "name", "FieldType": "Text", "FieldNameAlt": "What is your full name?"},
    {"FieldName": "age", "FieldType": "Text", "FieldNameAlt": "What is your age?"},
    {"FieldName": "children", "FieldType": "Text", "FieldNameAlt": "How many children do you have?",
     "FieldCondition": "values[{age}] >= 18"},
    {"FieldName": "sec2", "FieldType": "Section", "FieldValue": "Legal"},
    {"FieldName": "attorney", "FieldType": "Button", "FieldNameAlt": "Do you have an attorney?",
     "FieldOptions": {"Yes": "Yes", "No": "No"}},
    {"FieldName": "attorney_name", "FieldType": "Text", "FieldNameAlt": "What is your attorney's name?",
     "FieldCondition": "isYes({attorney})"},
    {"FieldName": "onset", "FieldType": "Button", "FieldNameAlt": "How did symptoms come on?",
     "FieldOptions": {"Suddenly": "Suddenly", "Gradually": "Gradually"}},
    {"FieldName": "onset_period", "FieldType": "Text", "FieldNameAlt": "Over what period?",
     "FieldCondition": "values[{onset}] == 'Gradually'"},
]


def graph():
    return FormGraph.from_fields_json(SYNTHETIC)


def nav_names(g, answers):
    return [f.name for f in g.navigable(answers)]


def test_visibility_numeric_gate():
    g = graph()
    assert "children" not in nav_names(g, {"age": "4"})       # child gate closed
    assert "children" in nav_names(g, {"age": "40"})          # child gate open


def test_visibility_yesno_and_choice_gates():
    g = graph()
    assert "attorney_name" not in nav_names(g, {"attorney": "No"})
    assert "attorney_name" in nav_names(g, {"attorney": "Yes"})
    assert "onset_period" not in nav_names(g, {"onset": "Suddenly"})
    assert "onset_period" in nav_names(g, {"onset": "Gradually"})


def test_headings_are_not_navigable():
    g = graph()
    assert "sec1" not in nav_names(g, {}) and "sec2" not in nav_names(g, {})


def test_relative_next_prev_steps_over_skipped():
    g = graph()
    answers = {"attorney": "No", "onset": "Suddenly", "age": "40"}
    # visible questions: name, age, children, attorney, onset  (attorney_name & onset_period skipped)
    cur = g.by_name["attorney"].id
    r = resolve_navigation(g, cur, answers, "next question")
    assert r.kind == "field" and r.field_name == "onset"      # stepped over skipped attorney_name
    r = resolve_navigation(g, cur, answers, "previous question")
    assert r.kind == "field" and r.field_name == "children"


def test_relative_n_back():
    g = graph()
    answers = {"age": "40"}
    cur = g.by_name["onset"].id
    r = resolve_navigation(g, cur, answers, "go back two questions")
    assert r.kind == "field" and r.field_name == "children"


def test_next_unanswered():
    g = graph()
    answers = {"name": "Ana", "age": "40"}   # children/attorney/onset still empty
    cur = g.by_name["age"].id
    r = resolve_navigation(g, cur, answers, "next unanswered question")
    assert r.kind == "field" and r.field_name == "children"


def test_next_unanswered_when_complete():
    g = graph()
    answers = {"name": "Ana", "age": "40", "children": "2", "attorney": "No",
               "onset": "Suddenly"}
    cur = g.by_name["onset"].id
    r = resolve_navigation(g, cur, answers, "next empty question")
    assert r.kind == "complete"


def test_goto_question_number():
    g = graph()
    answers = {"age": "40"}
    r = resolve_navigation(g, None, answers, "go to question 3")
    assert r.kind == "field" and r.field_name == "children"   # 1=name,2=age,3=children
    assert resolve_navigation(g, None, answers, "question 99").kind == "none"


def test_edges():
    g = graph()
    answers = {"age": "40"}
    first = g.navigable(answers)[0].id
    assert resolve_navigation(g, first, answers, "previous").kind == "edge"


def test_non_relative_defers_to_semantic():
    g = graph()
    r = resolve_navigation(g, None, {"age": "40"}, "fix my attorney's name")
    assert r.kind == "needs_semantic" and r.candidates      # index handed to the semantic seam


@pytest.mark.skipif(
    not os.path.exists("/storage/Work/pt-forms-management/forms/13. Injured Worker Questionaire/fields.json"),
    reason="real pt-forms fixture not present",
)
def test_real_injured_worker_form_loads_and_gates():
    path = "/storage/Work/pt-forms-management/forms/13. Injured Worker Questionaire/fields.json"
    g = FormGraph.from_fields_json(json.load(open(path)))
    assert len(g.fields) > 100
    # Q27 "attorney's name" is gated by isYes({Q26}); Q26 is "Do you have an attorney?"
    an = g.by_name.get("Q27")
    assert an is not None and an.condition and "Q26" in an.condition
    assert not g.is_visible(an, {"Q26": "No"})
    assert g.is_visible(an, {"Q26": "Yes"})
    # relative move works over the real graph
    nav = g.navigable({"Q26": "Yes"})
    r = resolve_navigation(g, nav[0].id, {"Q26": "Yes"}, "next question")
    assert r.kind == "field"

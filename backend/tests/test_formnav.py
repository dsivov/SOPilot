"""Form-navigation pilot — resolver + eligibility tests (PolarTie SmartForm #1)."""
import asyncio
import json
import os
import re

import numpy as np
import pytest

from sopilot.formnav import FormGraph, match_text, resolve_navigation, semantic_match

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


class _KeywordEmbedder:
    """Deterministic bag-of-words embedder so semantic ranking is testable offline."""

    def __init__(self, vocab):
        self.vocab = vocab

    def _vec(self, text):
        words = set(re.findall(r"[a-z']+", text.lower()))
        return np.array([1.0 if w in words else 0.0 for w in self.vocab], dtype=np.float32)

    async def embed(self, text):
        return self._vec(text)

    async def embed_many(self, texts):
        return [self._vec(t) for t in texts]


def _vocab_for(g):
    words = set()
    for f in g.fields:
        words |= set(re.findall(r"[a-z']+", match_text(f).lower()))
    words |= {"attorney", "name", "children", "period"}
    return sorted(words)


def test_semantic_match_by_meaning():
    g = graph()
    emb = _KeywordEmbedder(_vocab_for(g))
    answers = {"attorney": "Yes", "age": "40"}   # attorney_name is eligible
    cache = {}
    r = asyncio.run(semantic_match(emb, g, answers, "what is my attorney's name",
                                   embed_cache=cache, high=0.5, low=0.2))
    assert r.field_id == g.by_name["attorney_name"].id      # ranked the attorney field top
    assert r.confidence > 0
    assert len(cache) == len(g.navigable(answers))          # embeddings cached once


def test_semantic_match_none_for_nonsense():
    g = graph()
    emb = _KeywordEmbedder(_vocab_for(g))
    r = asyncio.run(semantic_match(emb, g, {"age": "40"}, "zzz qqq nonsense",
                                   embed_cache={}, high=0.6, low=0.3))
    assert r.kind == "none"


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

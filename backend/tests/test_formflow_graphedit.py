"""Constraint-graph AI-editing — the contradiction-checker (_validate_condition)."""
from sopilot.api.formflow import _validate_condition, _parse_ok

M = {
    "order": ["s0", "s1"],
    "stages": {
        "s0": {"fields": [
            {"name": "a", "cond": ""},
            {"name": "b", "cond": "isYes({a})"},
            {"name": "c", "cond": ""},
        ]},
        "s1": {"fields": [
            {"name": "d", "cond": "isYes({c})"},
        ]},
    },
}


def test_good_condition_is_valid():
    r = _validate_condition("isYes({a})", "b", M)
    assert r["valid"] and not r["errors"] and not r["warnings"]


def test_dangling_reference_invalid():
    r = _validate_condition("isYes({zzz})", "b", M)
    assert not r["valid"] and any("unknown field" in e for e in r["errors"])


def test_self_reference_invalid():
    r = _validate_condition("isYes({b})", "b", M)
    assert not r["valid"] and any("itself" in e for e in r["errors"])


def test_cycle_invalid():
    # a proposed to gate on b, but b already gates on a → cycle
    r = _validate_condition("isYes({b})", "a", M)
    assert not r["valid"] and any("cycle" in e for e in r["errors"])


def test_forward_reference_warns_but_valid():
    # b gating on d (d is asked after b) → valid but flagged
    r = _validate_condition("isYes({d})", "b", M)
    assert r["valid"] and any("forward reference" in w for w in r["warnings"])


def test_always_false_warns():
    r = _validate_condition("isYes({a}) and isNo({a})", "b", M)
    assert r["valid"] and any("always FALSE" in w for w in r["warnings"])


def test_clearing_is_valid():
    r = _validate_condition("", "b", M)
    assert r["valid"] and any("clears" in w for w in r["warnings"])


def test_bad_syntax_invalid():
    assert not _parse_ok("isYes({a}")[0]           # unbalanced
    r = _validate_condition("a and and b", "b", M)
    assert not r["valid"]

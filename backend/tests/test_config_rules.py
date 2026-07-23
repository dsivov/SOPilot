"""Config-management ruleset shape validation (the formal engine's three kinds)."""
from sopilot.api.configtools import _validate_rules


def rule_requires(**over) -> dict:
    return {"id": "r1", "kind": "requires", "when": "tool:send_email",
            "needs": "field:notification_service_url", "level": "error", "msg": "m", **over}


def test_valid_rules_pass():
    rules = [
        rule_requires(),
        {"id": "c1", "kind": "conflicts", "a": "tool:x", "b": "tool:y", "level": "warn", "msg": "m"},
        {"id": "e1", "kind": "enum", "field": "voice", "options": ["alloy", "echo"], "level": "error", "msg": "m"},
    ]
    assert _validate_rules(rules) is None
    assert _validate_rules([]) is None


def test_not_a_list_rejected():
    assert _validate_rules({"kind": "requires"}) is not None  # type: ignore[arg-type]


def test_unknown_kind_rejected():
    assert "kind" in _validate_rules([rule_requires(kind="magic")])


def test_bad_level_rejected():
    assert "level" in _validate_rules([rule_requires(level="fatal")])


def test_missing_id_rejected():
    assert "id" in _validate_rules([rule_requires(id=" ")])


def test_missing_kind_fields_rejected():
    assert "needs" in _validate_rules([rule_requires(needs="")])
    assert "b" in _validate_rules([{"id": "c", "kind": "conflicts", "a": "tool:x", "level": "warn", "msg": "m"}])
    assert "options" in _validate_rules([{"id": "e", "kind": "enum", "field": "voice", "level": "warn", "msg": "m"}])


def test_enum_options_must_be_list():
    assert "options" in _validate_rules(
        [{"id": "e", "kind": "enum", "field": "voice", "options": "alloy", "level": "warn", "msg": "m"}]
    )

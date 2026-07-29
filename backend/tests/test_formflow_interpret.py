"""Answer-capture (interpret) — option coercion helper."""
from sopilot.api.formflow import _coerce_option


def test_coerce_option_list_case_insensitive():
    assert _coerce_option("both", ["Left", "Right", "Both"]) == "Both"
    assert _coerce_option("LEFT", ["Left", "Right", "Both"]) == "Left"


def test_coerce_option_dict_key_or_label():
    opts = {"1": "Left side", "2": "Right side"}
    assert _coerce_option("right side", opts) == "2"   # matched by label → key
    assert _coerce_option("1", opts) == "1"            # matched by key


def test_coerce_option_passthrough_when_no_match_or_no_options():
    assert _coerce_option("45", None) == "45"
    assert _coerce_option("maybe", ["Yes", "No"]) == "maybe"   # unmatched → left as-is

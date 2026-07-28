"""Canonical constraint engine — the server-side home for the config DSL.

Until now the constraint engine lived only in the frontend (`rules.ts`), so the
backend could store rules/schema but never *evaluate* them — which is why the
copilot fell back to the LLM for warnings and form navigation grew its own
evaluator. This module is the one Python engine, reused by config write-back,
the copilot's warnings, and SmartForm navigation.

It adds, on top of the original set-ness rules:
  #1 conditional field visibility — an optional per-field ``condition`` (show-if);
  #2 value predicates — ``field:path == value`` / ``>= n`` / ``in [...]`` and a
     small boolean condition language (isYes/isNo/values[]) with comparisons;
  #3 server-side evaluation of both;
  #4 field order + sections + repeater markers in the schema model.

The condition language is deliberately the same one PolarTie forms use
(``isYes({Q26})``, ``values[{age}] >= 18``), so a form's FieldConditions map 1:1
onto SOPilot schema conditions.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field as dc_field
from typing import Any

# ---------------------------------------------------------------------------
# Value coercion + the sandboxed condition expression language (#1 + #2)
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"\{([^}]+)\}")            # {age} → 'age'
_ALLOWED_NODES = (
    ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not,
    ast.Compare, ast.Eq, ast.NotEq, ast.Gt, ast.GtE, ast.Lt, ast.LtE,
    ast.In, ast.NotIn, ast.Call, ast.Name, ast.Load, ast.Constant,
    ast.Subscript, ast.List, ast.Tuple,
)
_ALLOWED_NAMES = {"values", "value", "isYes", "isNo"}


def coerce(v: Any) -> Any:
    """Numeric-looking strings become numbers so ``x >= 0`` works; else unchanged."""
    if isinstance(v, str):
        s = v.strip()
        for cast in (int, float):
            try:
                return cast(s)
            except ValueError:
                continue
    return v


class _Values(dict):
    def __missing__(self, k):     # unanswered ⇒ None (comparisons then fail → False)
        return None


def evaluate_condition(expr: str | None, values: dict[str, Any]) -> bool:
    """Evaluate a show-if condition. Empty/None ⇒ visible; any error ⇒ not visible."""
    if not expr:
        return True
    raw = values or {}
    try:
        prepared = _TOKEN_RE.sub(lambda m: repr(m.group(1)), expr)
        tree = ast.parse(prepared, mode="eval")
        for node in ast.walk(tree):
            if not isinstance(node, _ALLOWED_NODES):
                return False
            if isinstance(node, ast.Name) and node.id not in _ALLOWED_NAMES:
                return False

        def _is(kind: set[str]):
            return lambda key: str(raw.get(key, "")).strip().lower() in kind

        env = {
            "__builtins__": {},
            "values": _Values({k: coerce(v) for k, v in raw.items()}),
            "value": _Values({k: coerce(v) for k, v in raw.items()}),
            "isYes": _is({"yes", "y", "true", "1"}),
            "isNo": _is({"no", "n", "false", "0"}),
        }
        return bool(eval(compile(tree, "<cond>", "eval"), env))  # noqa: S307 — sandboxed
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Rule predicates — set-ness (original) + value comparisons (#2)
# ---------------------------------------------------------------------------

_OPS = ["==", "!=", ">=", "<=", ">", "<"]
_CMP = {
    "==": lambda a, b: a == b, "!=": lambda a, b: a != b,
    ">": lambda a, b: a > b, ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
}


@dataclass
class State:
    """What predicates are evaluated against."""
    values: dict[str, Any] = dc_field(default_factory=dict)   # field path → value
    tools: set[str] = dc_field(default_factory=set)           # enabled tool names
    kb_modes: set[str] = dc_field(default_factory=set)        # active knowledge-base modes


def _lit(tok: str) -> Any:
    tok = tok.strip()
    if len(tok) >= 2 and tok[0] in "'\"" and tok[-1] == tok[0]:
        return tok[1:-1]
    return coerce(tok)


def _field_pred(rest: str, values: dict[str, Any]) -> bool:
    """`path` (set-ness) or `path <op> value` (value comparison, #2)."""
    for op in _OPS:
        if op in rest:
            path, val = rest.split(op, 1)
            cur = coerce(values.get(path.strip()))
            want = _lit(val)
            if cur is None:
                return False
            try:
                return _CMP[op](cur, want)
            except TypeError:
                return _CMP[op](str(cur), str(want))
    v = values.get(rest.strip())
    return v is not None and str(v).strip() != ""


def predicate_holds(pred: str, state: State) -> bool:
    """Does a single predicate string hold? Supports `a|b` any-of."""
    pred = (pred or "").strip()
    if not pred:
        return False
    if pred.startswith("tool:"):
        return any(t.strip() in state.tools for t in pred[5:].split("|"))
    if pred.startswith("kb_mode:"):
        return any(m.strip() in state.kb_modes for m in pred[8:].split("|"))
    if pred.startswith("field:"):
        # value predicates don't alternate; set-ness may (`field:a|b` = a or b set)
        rest = pred[6:]
        if any(op in rest for op in _OPS):
            return _field_pred(rest, state.values)
        return any(_field_pred(p.strip(), state.values) for p in rest.split("|"))
    return False


# ---------------------------------------------------------------------------
# Schema model — fields with order / section / condition / repeater (#4, #1)
# ---------------------------------------------------------------------------

@dataclass
class FieldDef:
    path: str
    ftype: str = "string"          # string|number|boolean|enum|section|repeater|…
    label: str | None = None
    options: list[str] | None = None
    required: bool = False
    condition: str | None = None   # #1 show-if expression
    section: str | None = None     # #4 heading this field falls under
    order: int = 0                 # #4 explicit order (else list order)
    repeat_of: str | None = None   # #4 repeater group this field belongs to

    @property
    def is_section(self) -> bool:
        return self.ftype == "section"


def parse_schema(schema: dict | None) -> list[FieldDef]:
    """Parse a config schema's fields into ordered FieldDefs, tracking sections."""
    out: list[FieldDef] = []
    section: str | None = None
    for i, f in enumerate((schema or {}).get("fields") or []):
        ftype = str(f.get("type", "string"))
        if ftype == "section":
            section = str(f.get("label") or f.get("path") or "")
        out.append(FieldDef(
            path=str(f.get("path", f"_f{i}")), ftype=ftype,
            label=f.get("label") or f.get("description"),
            options=f.get("options"), required=bool(f.get("required")),
            condition=f.get("condition") or None,
            section=None if ftype == "section" else (f.get("section") or section),
            order=int(f.get("order", i)), repeat_of=f.get("repeat_of") or None,
        ))
    out.sort(key=lambda d: d.order)
    return out


def visible_fields(schema: dict | None, values: dict[str, Any]) -> list[FieldDef]:
    """Fields whose show-if condition passes, in order (#1 + #4). Sections kept."""
    return [f for f in parse_schema(schema) if evaluate_condition(f.condition, values or {})]


# ---------------------------------------------------------------------------
# Rule evaluation (#3) — enum / requires / conflicts, value-aware
# ---------------------------------------------------------------------------

@dataclass
class RuleResult:
    rule: dict
    state: str                     # satisfied | violated | inactive
    msg: str = ""


def evaluate_rules(rules: list[dict] | None, state: State) -> list[RuleResult]:
    """Evaluate a ruleset against the state. Mirrors the frontend engine."""
    results: list[RuleResult] = []
    for r in rules or []:
        kind = r.get("kind")
        if kind == "enum":
            field, opts = r.get("field", ""), r.get("options") or []
            v = state.values.get(field)
            if v is None or str(v).strip() == "":
                results.append(RuleResult(r, "inactive"))
            elif str(v) in [str(o) for o in opts]:
                results.append(RuleResult(r, "satisfied"))
            else:
                results.append(RuleResult(r, "violated", r.get("msg", "")))
        elif kind == "requires":
            if not predicate_holds(r.get("when", ""), state):
                results.append(RuleResult(r, "inactive"))
            elif predicate_holds(r.get("needs", ""), state):
                results.append(RuleResult(r, "satisfied"))
            else:
                results.append(RuleResult(r, "violated", r.get("msg", "")))
        elif kind == "conflicts":
            if predicate_holds(r.get("a", ""), state) and predicate_holds(r.get("b", ""), state):
                results.append(RuleResult(r, "violated", r.get("msg", "")))
            else:
                results.append(RuleResult(r, "satisfied"))
    return results


def rule_findings(rules: list[dict] | None, state: State) -> list[dict]:
    """Just the violations, as {level, msg} — the shape the copilot/write-back want."""
    out = []
    for res in evaluate_rules(rules, state):
        if res.state == "violated":
            out.append({"level": res.rule.get("level", "error"), "msg": res.msg or "rule violated"})
    return out

"""Form navigation resolver — PolarTie SmartForm pilot (problem #1).

SOPilot as the navigation brain beside PolarTie's `forms_navigate`. This module
turns a pt-forms `fields.json` into a searchable **field graph**, computes the
currently-**eligible** (visible) ordered list of questions, and resolves a user's
navigation request to a single field.

Design decisions (see docs/POLARTIE_SMARTFORM_PROBLEMS.md):
  - **Read-from-pt-forms, single source of truth.** We never own the answers; the
    caller passes the current answer snapshot (fetched from pt-forms `get-fields`).
    This module is pure/stateless — no DB, no Redis — so it's trivially testable.
  - **Eligibility from the deterministic `FieldCondition`** (isYes/isNo/values[]),
    faithfully mirrored here; the fuzzy NL rule layer is out of scope for the pilot.
  - **Relative grammar first** ("next", "two back", "next unanswered", "question N");
    the semantic/content match ("the field where I said Ryanair") is a declared seam
    to be wired to embeddings in the SessionPool next.
  - Every resolution is meant to be logged as a precedent trace (learning layer) —
    the caller gets a structured NavResult it can persist.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field as dc_field
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Field graph
# ---------------------------------------------------------------------------

# Ack/heading field types that are not "questions" to jump between.
_HEADING_TYPES = {"Section"}
# Sentinel answer values pt-forms stores for non-answers — treated as "answered"
# only where noted; empty/missing means unanswered.
_EMPTY = {"", None}


@dataclass
class Field:
    """One node in the form's field graph (parsed from a pt-forms field object)."""

    id: int                       # 1-based sequential id (mirrors getIndexedSchema)
    name: str                     # FieldName, e.g. "Q27"
    label: str                    # FieldNameAlt — the human question/label
    ftype: str                    # FieldType: Text | Button | Repeater | Section | Info | …
    condition: str | None = None  # FieldCondition, e.g. "isYes({Q26})"
    section: str | None = None    # the Section title this field falls under
    options: dict | None = None   # FieldOptions (for Button/choice fields)

    @property
    def is_heading(self) -> bool:
        return self.ftype in _HEADING_TYPES


# {FieldName} → a quoted python string literal, so "values[{Q33}]" becomes
# "values['Q33']" and "isYes({Q26})" becomes "isYes('Q26')".
_TOKEN_RE = re.compile(r"\{([^}]+)\}")

# The only AST node types / names a FieldCondition may use (sandbox allow-list,
# mirrors the server's _validate_field_condition_ast).
_ALLOWED_NODES = (
    ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not,
    ast.Compare, ast.Eq, ast.NotEq, ast.Gt, ast.GtE, ast.Lt, ast.LtE,
    ast.Call, ast.Name, ast.Load, ast.Constant, ast.Subscript,
)
_ALLOWED_NAMES = {"values", "value", "isYes", "isNo"}


def _coerce(v: Any) -> Any:
    """Numeric-looking strings become numbers so `values[x] > 0` works; else as-is."""
    if isinstance(v, str):
        s = v.strip()
        try:
            return int(s)
        except ValueError:
            try:
                return float(s)
            except ValueError:
                return v
    return v


class FormGraph:
    """A parsed, condition-aware view of a form's fields."""

    def __init__(self, fields: list[Field]):
        self.fields = fields
        self.by_id = {f.id: f for f in fields}
        self.by_name = {f.name: f for f in fields}

    # ---- construction ----
    @classmethod
    def from_fields_json(cls, data: Any) -> "FormGraph":
        """Parse a pt-forms `fields.json` (a list of field objects, or {fields:[…]})."""
        raw = data.get("fields") if isinstance(data, dict) else data
        fields: list[Field] = []
        current_section: str | None = None
        for i, obj in enumerate(raw or []):
            ftype = str(obj.get("FieldType", "Text"))
            name = str(obj.get("FieldName", f"_f{i + 1}"))
            if ftype == "Section":
                current_section = str(obj.get("FieldValue") or obj.get("FieldNameAlt") or name)
            label = str(obj.get("FieldNameAlt") or obj.get("FieldValue") or name)
            fields.append(Field(
                id=i + 1, name=name, label=label, ftype=ftype,
                condition=(obj.get("FieldCondition") or None),
                section=None if ftype == "Section" else current_section,
                options=obj.get("FieldOptions") or None,
            ))
        return cls(fields)

    # ---- eligibility (skip logic) ----
    def _values_map(self, answers: dict[str, Any]) -> dict[str, Any]:
        """Answer values keyed by BOTH FieldName and numeric id (as str), coerced."""
        vm: dict[str, Any] = {}
        for name, val in (answers or {}).items():
            vm[name] = _coerce(val)
            f = self.by_name.get(name)
            if f is not None:
                vm[str(f.id)] = _coerce(val)
        return vm

    def is_visible(self, f: Field, answers: dict[str, Any]) -> bool:
        """A field is visible unless its FieldCondition evaluates false."""
        if not f.condition:
            return True
        return _eval_condition(f.condition, self._values_map(answers), answers)

    def is_answered(self, f: Field, answers: dict[str, Any]) -> bool:
        v = (answers or {}).get(f.name)
        return v not in _EMPTY and str(v).strip() != ""

    def navigable(self, answers: dict[str, Any]) -> list[Field]:
        """Ordered list of currently-eligible *questions* (visible, non-heading)."""
        return [f for f in self.fields if not f.is_heading and self.is_visible(f, answers)]

    def index_text(self, f: Field, answers: dict[str, Any]) -> str:
        """Embeddable text for the semantic index: label + section + current value."""
        val = (answers or {}).get(f.name)
        parts = [f.label]
        if f.section:
            parts.append(f"[{f.section}]")
        if val not in _EMPTY and str(val).strip():
            parts.append(f"answer: {val}")
        return " · ".join(parts)


def _eval_condition(expr: str, values: dict[str, Any], raw: dict[str, Any]) -> bool:
    """Safely evaluate a FieldCondition. Any error / missing value ⇒ not visible."""
    try:
        prepared = _TOKEN_RE.sub(lambda m: repr(m.group(1)), expr)
        tree = ast.parse(prepared, mode="eval")
        for node in ast.walk(tree):
            if not isinstance(node, _ALLOWED_NODES):
                return False
            if isinstance(node, ast.Name) and node.id not in _ALLOWED_NAMES:
                return False

        def _is(kind: set[str]) -> Callable[[str], bool]:
            def f(key: str) -> bool:
                return str(raw.get(key, "")).strip().lower() in kind
            return f

        class _V(dict):
            def __missing__(self, k):  # unanswered ⇒ None (comparisons then fail → False)
                return None

        env = {
            "__builtins__": {},
            "values": _V(values), "value": _V(values),
            "isYes": _is({"yes", "y", "true", "1"}),
            "isNo": _is({"no", "n", "false", "0"}),
        }
        return bool(eval(compile(tree, "<cond>", "eval"), env))  # noqa: S307 — sandboxed
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Navigation resolver
# ---------------------------------------------------------------------------

@dataclass
class NavResult:
    kind: str                    # field | edge | complete | none | needs_semantic
    field_id: int | None = None
    field_name: str | None = None
    label: str | None = None
    message: str = ""
    request: str = ""            # echoed for precedent logging
    candidates: list[int] = dc_field(default_factory=list)  # for future disambiguation


_WORD_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "a": 1, "an": 1}


def _num(tok: str) -> int | None:
    tok = tok.strip().lower()
    if tok.isdigit():
        return int(tok)
    return _WORD_NUM.get(tok)


def _parse_relative(req: str):
    """Return a relative-move intent tuple, or None if it isn't a relative request."""
    r = " ".join(req.lower().split())
    # unanswered / empty
    if re.search(r"\b(next|following)\b.*\b(unanswered|empty|blank|missing|unfilled)\b", r):
        return ("next_unanswered",)
    if re.search(r"\b(first|start|begin)\w*\b.*\b(unanswered|empty|blank|missing|question)\b", r) \
            or r in {"start", "beginning", "the start", "back to the start", "first question"}:
        return ("first_unanswered",)
    # go to question N / field N / number N
    m = re.search(r"\b(?:question|field|number|q|#)\s*(\d+)\b", r) or re.search(r"\bgo to\b.*?(\d+)\b", r)
    if m:
        return ("goto", int(m.group(1)))
    # N back / back N / N forward / forward N
    m = re.search(r"\b(\d+|one|two|three|four|five)\s+(?:questions?\s+)?(back|backward|backwards|forward|ahead|forwards)\b", r) \
        or re.search(r"\b(back|backward|forward|ahead)\s+(\d+|one|two|three|four|five)\b", r)
    if m:
        g = m.groups()
        n = _num(g[0]) if _num(g[0]) is not None else _num(g[1])
        direction = g[1] if _num(g[0]) is not None else g[0]
        if n is not None:
            return ("rel", -n if direction.startswith("back") else n)
    # plain next / previous / back
    if re.search(r"\b(previous|prev|back|last one|go back)\b", r):
        return ("rel", -1)
    if re.search(r"\b(next|forward|continue|following|skip ahead)\b", r):
        return ("rel", 1)
    return None


def _find_index(nav: list[Field], current_field_id: int | None) -> int:
    """Position of current field in the navigable list; -1 if not present/None."""
    for i, f in enumerate(nav):
        if f.id == current_field_id:
            return i
    return -1


def resolve_navigation(graph: FormGraph, current_field_id: int | None,
                       answers: dict[str, Any], request: str) -> NavResult:
    """Resolve a navigation request against the currently-eligible field list.

    Relative moves are handled here; anything that isn't relative is returned as
    `needs_semantic` for the (to-be-wired) embedding-based content match.
    """
    nav = graph.navigable(answers or {})
    if not nav:
        return NavResult(kind="none", message="This form has no answerable questions.", request=request)
    cur = _find_index(nav, current_field_id)
    intent = _parse_relative(request)

    def at(i: int) -> NavResult:
        f = nav[i]
        return NavResult(kind="field", field_id=f.id, field_name=f.name, label=f.label, request=request)

    if intent is None:
        # content/description match — semantic resolver seam (embeddings in SessionPool)
        return NavResult(kind="needs_semantic",
                         message="Not a relative move — resolve by meaning against the field index.",
                         request=request, candidates=[f.id for f in nav])

    if intent[0] == "rel":
        if cur < 0:  # unknown current position → step from the ends
            cur = -1 if intent[1] > 0 else len(nav)
        j = cur + intent[1]
        if j < 0 or j >= len(nav):
            return NavResult(kind="edge",
                             message="You're already at the first question." if intent[1] < 0
                                     else "You're already at the last question.", request=request)
        return at(j)

    if intent[0] in ("next_unanswered", "first_unanswered"):
        start = cur + 1 if intent[0] == "next_unanswered" and cur >= 0 else 0
        for i in range(start, len(nav)):
            if not graph.is_answered(nav[i], answers or {}):
                return at(i)
        return NavResult(kind="complete", message="All questions are answered.", request=request)

    if intent[0] == "goto":
        n = intent[1]  # 1-based over the navigable (eligible) list — NOTE: display-number
        if 1 <= n <= len(nav):                                    # mapping is a follow-up.
            return at(n - 1)
        return NavResult(kind="none", message=f"There is no question number {n} in this form.", request=request)

    return NavResult(kind="needs_semantic", request=request)  # unreachable, defensive


# ---------------------------------------------------------------------------
# Answer source (read-from-pt-forms) — interface + seam
# ---------------------------------------------------------------------------

class AnswerSource:
    """Where the current answer snapshot comes from. pt-forms is the source of truth."""

    async def get_answers(self, submission_uuid: str) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError


class PtFormsAnswerSource(AnswerSource):
    """Reads the live answers from pt-forms `GET /api/fill/{uuid}/get-fields`.

    Kept as a thin seam for the pilot — wired to httpx when we connect to a live
    forms server. The resolver itself never depends on this (tests pass answers in).
    """

    def __init__(self, base_url: str, browser_session_token: str = ""):
        self.base_url = base_url.rstrip("/")
        self.token = browser_session_token

    async def get_answers(self, submission_uuid: str) -> dict[str, Any]:  # pragma: no cover
        import httpx
        headers = {"X-Browser-Session-Token": self.token} if self.token else {}
        async with httpx.AsyncClient(timeout=10) as hc:
            r = await hc.get(f"{self.base_url}/api/fill/{submission_uuid}/get-fields", headers=headers)
            r.raise_for_status()
            body = r.json()
        # get-fields returns the indexed schema + current values; we only need values.
        return body.get("values") or body.get("data") or {}

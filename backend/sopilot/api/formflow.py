"""Form-flow supervisor — the SmartForm runtime spike (chosen approach).

The realtime voice agent calls this at each Next/Prev boundary with the current
field + the answers snapshot (from pt-forms, the source of truth). The supervisor:

  1. resolves current field → its STAGE deterministically (the __map__ block);
  2. picks the next VISIBLE, unanswered field — gating enforced by constraints.py
     (a skipped field is never surfaced), crossing into the next stage if needed;
  3. asks a STRONG model, given the stage's PLAYBOOK block + the flow block + the
     relevant answers, to prepare ONE compact instruction for the realtime agent.

SOP + prompt blocks are storage/structure; this drives them deterministically and
uses the strong model only to phrase the prepared turn. No answers are stored here.
"""
from __future__ import annotations

import json
import re

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..constraints import evaluate_condition
from ..db import get_db
from ..tenancy import Scope, resolve_scope

router = APIRouter(prefix="/formflow", tags=["formflow"])

# pt-forms get-fields addresses fields by numeric id and surfaces repeater
# instances as "<id>[i]"; strip the instance suffix down to the base id.
_ID_SUFFIX = re.compile(r"^(\d+)(?:\[\d+\])?$")


class FormSource(BaseModel):
    """Live pt-forms submission to pull the answer snapshot from (get-fields)."""
    base_url: str
    submission_uuid: str
    browser_session_token: str | None = None


class PrepareRequest(BaseModel):
    current_field: str | None = None   # where the user is now (FieldName or numeric id); None = start
    answers: dict = {}                  # inline snapshot (FieldName → value) — used when `source` is unset
    source: FormSource | None = None    # when set, answers are read live from pt-forms get-fields
    phrase: bool = True                 # False = return the raw next field (skip the strong model) + context size


async def _load_manifest(db, scope) -> dict | None:
    """Find & parse the form's __map__ block (the deterministic stage↔field index)."""
    from .prompt_blocks import resolve_published_blocks
    from ..models import PromptBlock
    from sqlalchemy import select
    names = (await db.execute(select(PromptBlock.name).where(
        PromptBlock.tenant_id == scope.tenant_id, PromptBlock.project_id == scope.project_id))).scalars().all()
    map_names = [n for n in names if n.endswith(".__map__")]
    if not map_names:
        return None
    resolved, _ = await resolve_published_blocks(db, scope, {map_names[0]})
    raw = resolved.get(map_names[0], {}).get("content")
    try:
        return json.loads(raw) if raw else None
    except Exception:
        return None


_INST_RE = re.compile(r"^(.*)\[(\d+)\]$")


def _split_instance(key):
    """'MedName[2]' → ('MedName', 2); 'age' → ('age', None)."""
    m = _INST_RE.match(str(key))
    return (m.group(1), int(m.group(2))) if m else (str(key), None)


def _instance_view(answers: dict, members: set, inst: int) -> dict:
    """Values for evaluating a repeat member's condition AT one instance: group members
    resolve to their <name>[inst] value; everything else stays as its flat value."""
    view = {k: v for k, v in answers.items() if "[" not in str(k)}
    for m in members:
        view[m] = answers.get(f"{m}[{inst}]", "")
    return view


def _repeat_next(members: list, repeater: dict, answers: dict):
    """The next field to ask inside a repeat group, or None when the group is done.

    Serves each unanswered, visible member at the current instance; when an instance is
    complete, serves the repeater itself ('add another?'); a Yes answer opens the next
    instance, a No ends the group. Instance answers are keyed <name>[i].
    """
    if repeater.get("cond") and not evaluate_condition(repeater["cond"], answers):
        return None  # whole group hidden
    mset = {m["name"] for m in members}
    i = 0
    while True:
        view = _instance_view(answers, mset, i)
        for m in members:
            if m.get("cond") and not evaluate_condition(m["cond"], view):
                continue
            key = f'{m["name"]}[{i}]'
            if str(answers.get(key, "")).strip() == "":
                return {"name": key, "id": m.get("id"), "label": m.get("label"), "repeat_instance": i}
        addkey = f'{repeater["name"]}[{i}]'
        if str(answers.get(addkey, "")).strip() == "":
            return {"name": addkey, "id": repeater.get("id"), "label": repeater.get("label"),
                    "repeat_instance": i, "is_repeater": True}
        if str(answers.get(addkey)).strip().lower() in ("yes", "y", "true", "1"):
            i += 1
            continue
        return None  # "No" → group complete


def _visible_unanswered(manifest: dict, answers: dict, start_stage: str | None, start_after: str | None):
    """First visible, unanswered field at/after the cursor, across stages in order.
    Repeat groups (a Repeater + its members) are served instance-by-instance.
    Returns (stage_id, field) or (None, None) when the form is complete."""
    order = manifest.get("order", [])
    stages = manifest.get("stages", {})
    started = start_stage is None
    cur_base, cur_inst = _split_instance(start_after) if start_after else (None, None)
    # an instance cursor (mid-repeat) → process from the start; answered-checks skip filled fields
    passed_cursor = (start_after is None) or (cur_inst is not None)
    consumed: set = set()
    for sid in order:
        if not started:
            if sid == start_stage:
                started = True
            else:
                continue
        fields = stages.get(sid, {}).get("fields", [])
        for f in fields:
            name = f["name"]
            grp = f.get("repeat_group") or (name if f.get("repeater") else None)
            if grp is not None:                       # a repeat member or the repeater itself
                if grp in consumed:
                    continue
                if not passed_cursor:
                    if cur_base == name or (f.get("repeater") and cur_base in f.get("members", [])):
                        passed_cursor = True
                    else:
                        continue
                membs = [x for x in fields if x.get("repeat_group") == grp]
                repf = next((x for x in fields if x.get("name") == grp and x.get("repeater")), None)
                consumed.add(grp)
                if repf is None:
                    continue
                nxt = _repeat_next(membs, repf, answers)
                if nxt is not None:
                    return sid, nxt
                continue
            if not passed_cursor:
                if name == cur_base:
                    passed_cursor = True
                continue
            if not evaluate_condition(f.get("cond"), answers):        # gating (constraints.py)
                continue
            v = answers.get(name)
            if v is None or str(v).strip() == "":
                return sid, f
    return None, None


# Values pt-forms stores for a non-answer (a skipped field is recorded as one of
# these); they must NOT be treated as real answers when reconciling visibility.
_SENTINELS = {"", "not applicable", "n/a", "na", "unknown", "n.a."}


def _is_real_answer(v) -> bool:
    return v is not None and str(v).strip().lower() not in _SENTINELS


def _reconcile_stale(manifest: dict, answers: dict) -> tuple[list[dict], dict]:
    """Find fields that are ANSWERED but now HIDDEN, and void them — to a fixpoint.

    A backward edit (changing an earlier answer) can hide a field that already holds a
    real answer; that stale value would otherwise survive into the PDF. We void it
    ("not applicable"), iterating until stable so cascades resolve. Repeat groups are
    handled per instance: if the whole repeater is hidden, every <member>[i] and the
    repeater's own answer are voided; otherwise each instance is checked on its own
    values. Returns (voided_fields, reconciled_answers).
    """
    ans = dict(answers)
    fields = [f for s in manifest.get("stages", {}).values() for f in s.get("fields", [])]
    # repeat metadata from the manifest
    repeaters = {f["name"]: f for f in fields if f.get("repeater")}
    members_of = {rn: set(rf.get("members", [])) for rn, rf in repeaters.items()}
    member_names = {m for ms in members_of.values() for m in ms}

    def _void(key, f):
        voided.append({"name": key, "id": f.get("id"), "was": ans.get(key)})
        ans[key] = "not applicable"

    voided: list[dict] = []
    changed = True
    while changed:
        changed = False
        for f in fields:
            name, cond = f["name"], f.get("cond")
            if name in member_names:
                continue  # repeat members handled per instance below
            if not cond:
                continue
            if _is_real_answer(ans.get(name)) and not evaluate_condition(cond, ans):
                _void(name, f); changed = True

        # repeat groups, per instance
        for rn, rep in repeaters.items():
            members = members_of[rn]
            rep_hidden = bool(rep.get("cond")) and not evaluate_condition(rep["cond"], ans)
            mem_fields = {m: next((x for x in fields if x["name"] == m), {}) for m in members}
            # every instance index seen for any member or the repeater's own [i] key
            insts = set()
            for k in ans:
                b, i = _split_instance(k)
                if i is not None and (b in members or b == rn):
                    insts.add(i)
            for i in sorted(insts):
                view = _instance_view(ans, members, i)
                for m in members:
                    key = f"{m}[{i}]"
                    if not _is_real_answer(ans.get(key)):
                        continue
                    mcond = mem_fields[m].get("cond")
                    if rep_hidden or (mcond and not evaluate_condition(mcond, view)):
                        _void(key, mem_fields[m]); changed = True
            if rep_hidden and _is_real_answer(ans.get(rn)):
                _void(rn, rep); changed = True
    return voided, ans


def _stage_of(manifest: dict, field_name: str | None) -> str | None:
    if not field_name:
        return None
    for sid, s in manifest.get("stages", {}).items():
        if any(f["name"] == field_name for f in s.get("fields", [])):
            return sid
    return None


def _id_to_name(manifest: dict) -> dict[int, str]:
    """pt-forms numeric id → our FieldName (from the ingested __map__ block)."""
    out: dict[int, str] = {}
    for s in manifest.get("stages", {}).values():
        for f in s.get("fields", []):
            if f.get("id") is not None:
                out[int(f["id"])] = f["name"]
    return out


def _live_answers(get_fields_body: dict, manifest: dict) -> dict:
    """Convert pt-forms get-fields id-keyed `values` → FieldName-keyed answers.

    get-fields strips FieldName, so the id→name bridge comes from our manifest.
    Repeater instances ('<id>[i]') collapse to the base field for the pilot.
    """
    id2name = _id_to_name(manifest)
    values = get_fields_body.get("values") or get_fields_body.get("data") or {}
    answers: dict = {}
    for k, v in values.items():
        m = _ID_SUFFIX.match(str(k))
        name = id2name.get(int(m.group(1))) if m else None
        if name is None:
            name = str(k)  # already a FieldName, or an unknown id — pass through
        answers[name] = v
    return answers


def _normalize_field(manifest: dict, current: str | None) -> str | None:
    """Accept the cursor as a FieldName or a numeric get-fields id; return the FieldName."""
    if current is None:
        return None
    m = _ID_SUFFIX.match(str(current))
    if m:
        return _id_to_name(manifest).get(int(m.group(1)), str(current))
    return str(current)


async def _fetch_get_fields(source: FormSource) -> dict:
    import httpx
    headers = {"X-Browser-Session-Token": source.browser_session_token} if source.browser_session_token else {}
    url = f"{source.base_url.rstrip('/')}/api/fill/{source.submission_uuid}/get-fields"
    async with httpx.AsyncClient(timeout=10) as hc:
        r = await hc.get(url, headers=headers)
        r.raise_for_status()
        return r.json()


async def _push_set_fields(source: FormSource, id_values: dict) -> None:
    """Write id-keyed values back to pt-forms (set-fields) — used to void stale answers."""
    import httpx
    headers = {"X-Browser-Session-Token": source.browser_session_token} if source.browser_session_token else {}
    url = f"{source.base_url.rstrip('/')}/api/fill/{source.submission_uuid}/set-fields"
    async with httpx.AsyncClient(timeout=10) as hc:
        r = await hc.put(url, headers=headers, json={"values": id_values})
        r.raise_for_status()


@router.post("/prepare")
async def prepare(req: PrepareRequest, scope: Scope = Depends(resolve_scope),
                  db: AsyncSession = Depends(get_db)) -> dict:
    from .prompt_blocks import resolve_published_blocks
    from ..bench.llm import client
    from ..config import get_settings

    manifest = await _load_manifest(db, scope)
    if not manifest:
        return {"error": "no form published for this project (missing __map__ block)"}

    # Live pt-forms: pull the answer snapshot from get-fields (source of truth),
    # converting its id-keyed values to the FieldName-keyed answers the driver uses.
    answers = req.answers or {}
    live = False
    if req.source is not None:
        try:
            body = await _fetch_get_fields(req.source)
        except Exception as e:
            return {"error": f"could not read pt-forms get-fields ({type(e).__name__}: {e})"}
        answers = _live_answers(body, manifest)
        live = True

    current_field = _normalize_field(manifest, req.current_field)
    cur_stage = _stage_of(manifest, current_field)
    stage_id, field = _visible_unanswered(manifest, answers, cur_stage, current_field)
    if field is None:
        return {"done": True, "message": "All visible questions are answered — ready to submit."}

    st = manifest["stages"][stage_id]
    # load the stage playbook + the global flow block (strong-model context)
    want = {st["block"], manifest.get("flow_block", "")}
    want.discard("")
    blocks, _ = await resolve_published_blocks(db, scope, want)
    playbook = blocks.get(st["block"], {}).get("content", "")
    flow = blocks.get(manifest.get("flow_block", ""), {}).get("content", "")

    # only the answers this stage references (keep the strong-model context tight)
    stage_field_names = {f["name"] for f in st["fields"]}
    relevant = {k: v for k, v in answers.items() if k in stage_field_names}

    user = (
        f"CURRENT STAGE: {st['title']}\n\n{playbook}\n\n"
        f"ANSWERS SO FAR (this stage):\n{json.dumps(relevant, ensure_ascii=False) or '(none)'}\n\n"
        f"NEXT FIELD TO ASK: {field['name']} — {field.get('label') or ''}\n\n"
        "Prepare the single compact instruction the realtime agent should speak to ask this field. "
        "Return ONLY the question text (plus a requested-change note if any) — no field id, no metadata."
    )
    # Deterministic-only path (benchmark / no-LLM fallback consumers): return the raw
    # field plus the strong-model context size that WOULD be sent, without calling it.
    if not req.phrase:
        return {"stage": st["title"], "stage_id": stage_id, "next_field": field["name"],
                "next_field_id": field.get("id"), "label": field.get("label"),
                "instruction": field.get("label") or field["name"],
                "context_chars": len(flow) + len(user), "source": "live" if live else "inline",
                "phrased": False, "done": False}
    try:
        res = await client().chat.completions.create(
            model=get_settings().builder_model,
            messages=[{"role": "system", "content": flow}, {"role": "user", "content": user}],
            temperature=0.2, max_tokens=200)
        instruction = (res.choices[0].message.content or "").strip()
    except Exception as e:
        instruction = field.get("label") or field["name"]  # fallback: raw label
        return {"stage": st["title"], "stage_id": stage_id, "next_field": field["name"],
                "next_field_id": field.get("id"), "label": field.get("label"), "instruction": instruction,
                "source": "live" if live else "inline",
                "warning": f"supervisor model unavailable ({type(e).__name__}) — raw label used"}

    return {"stage": st["title"], "stage_id": stage_id, "next_field": field["name"],
            "next_field_id": field.get("id"), "label": field.get("label"), "instruction": instruction,
            "source": "live" if live else "inline", "done": False}


class ReconcileRequest(BaseModel):
    answers: dict = {}                  # inline snapshot (FieldName → value) — used when `source` is unset
    source: FormSource | None = None    # live pt-forms; answers read (and voids written back) via get/set-fields
    apply: bool = True                  # when live + apply, write the voids back to pt-forms; else dry-run


@router.post("/reconcile")
async def reconcile(req: ReconcileRequest, scope: Scope = Depends(resolve_scope),
                    db: AsyncSession = Depends(get_db)) -> dict:
    """Void answers that a later edit has hidden (backward edit-propagation).

    A field answered while visible can be hidden by a subsequent change to a
    controlling answer; its stale value would otherwise persist into the PDF. This
    recomputes visibility with constraints.py and voids any answered-but-hidden
    field (to a fixpoint, so cascades are handled). With a live source + apply, the
    voids are written back to pt-forms as "not applicable".
    """
    manifest = await _load_manifest(db, scope)
    if not manifest:
        return {"error": "no form published for this project (missing __map__ block)"}

    answers = req.answers or {}
    live = False
    if req.source is not None:
        try:
            body = await _fetch_get_fields(req.source)
        except Exception as e:
            return {"error": f"could not read pt-forms get-fields ({type(e).__name__}: {e})"}
        answers = _live_answers(body, manifest)
        live = True

    voided, _ = _reconcile_stale(manifest, answers)

    applied = False
    if live and req.apply and voided:
        id_values = {str(v["id"]): "not applicable" for v in voided if v.get("id") is not None}
        try:
            await _push_set_fields(req.source, id_values)
            applied = True
        except Exception as e:
            return {"error": f"could not write set-fields to pt-forms ({type(e).__name__}: {e})",
                    "would_void": voided}

    return {"voided": voided, "count": len(voided),
            "applied": applied, "source": "live" if live else "inline"}


class InterpretRequest(BaseModel):
    raw_answer: str                     # what the patient said (messy / colloquial / any language)
    field_name: str | None = None       # for stage/flow context + optional live write-back
    label: str = ""                     # the question (falls back to the manifest / get-fields)
    ftype: str = "Text"                 # Number / Boolean / Button(choice) / Date / Text …
    options: object | None = None       # choice options (list or {key: label})
    source: FormSource | None = None    # optional live: pull the field's schema + write the value back
    apply: bool = False                 # when live + apply, write the interpreted value back to pt-forms


def _coerce_option(value: str, options) -> str:
    """Map a produced value to the exact stored option (key or case-insensitive label)."""
    if not options:
        return value
    t = str(value).strip().lower()
    if isinstance(options, dict):
        for k in options:
            if str(k).strip().lower() == t:
                return str(k)
        for k, lab in options.items():
            if str(lab).strip().lower() == t:
                return str(k)
    elif isinstance(options, list):
        for o in options:
            if str(o).strip().lower() == t:
                return str(o)
    return value


@router.post("/interpret")
async def interpret(req: InterpretRequest, scope: Scope = Depends(resolve_scope),
                    db: AsyncSession = Depends(get_db)) -> dict:
    """Answer capture — turn a messy spoken answer into the exact value to store.

    The strong supervisor normalizes one patient utterance to the field's format
    (numbers as digits, dates as YYYY-MM-DD, yes/no, EXACTLY one option for a choice,
    'don't know' → unknown), using the SOP flow block's coercion rules. This is the
    piece pt-forms leaves to the weak realtime model.
    """
    import datetime as _dt
    from .prompt_blocks import resolve_published_blocks
    from ..bench.llm import client
    from ..config import get_settings

    label, ftype, options = req.label, req.ftype, req.options
    live = False
    if req.source is not None:
        manifest0 = await _load_manifest(db, scope)
        fid = None
        if manifest0 and req.field_name:
            fid = next((f.get("id") for s in manifest0.get("stages", {}).values()
                        for f in s.get("fields", []) if f["name"] == req.field_name), None)
        try:
            body = await _fetch_get_fields(req.source)
            for f in body.get("fields", []):
                if fid is not None and str(f.get("id")) == str(fid):
                    label = label or str(f.get("FieldNameAlt") or "")
                    ftype = f.get("FieldType") or ftype
                    options = f.get("FieldOptions") or options
                    break
            live = True
        except Exception as e:
            return {"error": f"could not read pt-forms get-fields ({type(e).__name__}: {e})"}

    # reuse the SOP flow block's coercion policy as the system context
    manifest = await _load_manifest(db, scope)
    flow = ""
    if manifest and manifest.get("flow_block"):
        blocks, _ = await resolve_published_blocks(db, scope, {manifest["flow_block"]})
        flow = blocks.get(manifest["flow_block"], {}).get("content", "")

    opt_txt = ""
    if options:
        opts = list(options.values()) if isinstance(options, dict) else options
        opt_txt = f"\nCHOICES (return EXACTLY one, verbatim): {', '.join(map(str, opts))}"
    today = _dt.date.today().isoformat()
    system = (
        (flow + "\n\n" if flow else "") +
        "You capture ONE patient answer for a form field and output the exact value to STORE.\n"
        f"Today is {today}. Rules: numbers as digits; dates as YYYY-MM-DD (resolve relative dates against today); "
        "yes/no questions → 'Yes' or 'No'; a choice field → EXACTLY one of the given choices; "
        "\"don't know\" / \"not sure\" / refusals → 'unknown'; a name → keep it (transliterate to Latin letters); "
        "otherwise the answer as-is, cleaned. Output ONLY the value — no punctuation, no explanation.")
    user = f"QUESTION: {label or req.field_name}\nFIELD TYPE: {ftype}{opt_txt}\nPATIENT SAID: \"{req.raw_answer}\"\nVALUE TO STORE:"
    try:
        res = await client().chat.completions.create(
            model=get_settings().builder_model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0, max_tokens=40)
        value = (res.choices[0].message.content or "").strip().strip('".')
    except Exception as e:
        return {"error": f"supervisor model unavailable ({type(e).__name__})", "value": req.raw_answer}

    value = _coerce_option(value, options)
    applied = False
    if live and req.apply and req.field_name:
        m = await _load_manifest(db, scope)
        fid = next((f.get("id") for s in (m or {}).get("stages", {}).values()
                    for f in s.get("fields", []) if f["name"] == req.field_name), None)
        if fid is not None:
            try:
                await _push_set_fields(req.source, {str(fid): value})
                applied = True
            except Exception:
                pass
    return {"value": value, "field_name": req.field_name, "applied": applied, "source": "live" if live else "inline"}


# ---------------------------------------------------------------------------
# Constraint-graph view — the form's dependency graph as nodes/edges (+ health).
# The first slice of the constraint-graph Studio: visualize, then edit (AI +
# visual) with contradiction-checking, then a live execution trace.
# ---------------------------------------------------------------------------
_COND_TOKEN = re.compile(r"\{([^}]+)\}")


@router.get("/graph")
async def graph(scope: Scope = Depends(resolve_scope), db: AsyncSession = Depends(get_db)) -> dict:
    """The published form's dependency graph: fields → nodes, show-if conditions → edges.

    An edge Y → X means "field X is shown only when Y's value satisfies X's condition".
    Also returns a health block (dangling references, cross-stage deps, top controllers)
    — the seed of contradiction-checking.
    """
    manifest = await _load_manifest(db, scope)
    if not manifest:
        return {"error": "no form published for this project (missing __map__ block)"}

    stage_of: dict[str, str] = {}
    stage_title: dict[str, str] = {}
    fields: list[dict] = []
    for sid, s in manifest.get("stages", {}).items():
        for f in s.get("fields", []):
            stage_of[f["name"]] = sid
            stage_title[sid] = s.get("title", sid)
            fields.append(f)
    names = {f["name"] for f in fields}

    nodes = [{
        "name": f["name"], "id": f.get("id"), "label": f.get("label") or f["name"],
        "stage": stage_of[f["name"]], "stage_title": stage_title[stage_of[f["name"]]],
        "conditional": bool(f.get("cond")),
        "repeat_group": f.get("repeat_group"), "repeater": bool(f.get("repeater")),
    } for f in fields]

    edges: list[dict] = []
    dangling: list[dict] = []
    from collections import Counter
    fanout: Counter = Counter()
    for f in fields:
        cond = f.get("cond") or ""
        for tok in dict.fromkeys(_COND_TOKEN.findall(cond)):   # unique, in order
            if tok in names:
                edges.append({"src": tok, "dst": f["name"], "expr": cond,
                              "cross_stage": stage_of[tok] != stage_of[f["name"]]})
                fanout[tok] += 1
            else:
                dangling.append({"field": f["name"], "missing_ref": tok, "expr": cond})

    stats = {
        "fields": len(fields),
        "conditional_fields": sum(1 for f in fields if f.get("cond")),
        "edges": len(edges),
        "cross_stage_edges": sum(1 for e in edges if e["cross_stage"]),
        "stages": len(manifest.get("stages", {})),
        "dangling_refs": len(dangling),
        "top_controllers": [{"name": n, "controls": c} for n, c in fanout.most_common(8)],
    }
    stages = [{"id": sid, "title": stage_title.get(sid, sid)} for sid in manifest.get("order", [])]
    return {"form": manifest.get("form"), "nodes": nodes, "edges": edges,
            "stages": stages, "stats": stats, "health": {"dangling": dangling}}


# ---------------------------------------------------------------------------
# Constraint-graph Studio — step 2: AI-assisted rule editing with contradiction-checking.
# A natural-language instruction → a proposed FieldCondition → validated by the same
# grammar constraints.py evaluates, then returned as a diff (never applied unsafely).
# ---------------------------------------------------------------------------
import ast as _ast
from ..constraints import _ALLOWED_NODES as _AN, _ALLOWED_NAMES as _ANM, _TOKEN_RE as _TR


def _parse_ok(expr: str) -> tuple[bool, str]:
    """True if expr is a well-formed FieldCondition in the sandboxed grammar."""
    if not expr:
        return True, ""
    try:
        prepared = _TR.sub(lambda m: repr(m.group(1)), expr)
        tree = _ast.parse(prepared, mode="eval")
        for node in _ast.walk(tree):
            if not isinstance(node, _AN):
                return False, f"disallowed syntax ({type(node).__name__})"
            if isinstance(node, _ast.Name) and node.id not in _ANM:
                return False, f"unknown function/name '{node.id}'"
        return True, ""
    except Exception as e:
        return False, f"parse error: {e}"


def _global_order(manifest: dict) -> dict:
    idx, i = {}, 0
    for sid in manifest.get("order", []):
        for f in manifest.get("stages", {}).get(sid, {}).get("fields", []):
            idx[f["name"]] = i
            i += 1
    return idx


def _validate_condition(expr: str, field: str, manifest: dict) -> dict:
    """Contradiction-check a proposed show-if for `field`: syntax, dangling refs,
    self-reference, cycles, forward references, and always-true/false rules."""
    fields = [f for s in manifest.get("stages", {}).values() for f in s.get("fields", [])]
    cond_of = {f["name"]: (f.get("cond") or "") for f in fields}
    names = set(cond_of)
    order = _global_order(manifest)
    warnings: list[str] = []

    ok, err = _parse_ok(expr)
    if not ok:
        return {"valid": False, "errors": [err], "warnings": []}
    if not expr:
        return {"valid": True, "errors": [], "warnings": ["clears the condition (field always shown)"]}

    refs = set(_TR.findall(expr))
    errors: list[str] = []
    dangling = sorted(refs - names)
    if dangling:
        errors.append(f"references unknown field(s): {', '.join(dangling)}")
    if field in refs:
        errors.append("references itself (a field cannot gate on its own value)")
    # one-hop cycle: a referenced field's own condition references `field` back
    for r in refs & names:
        if field and field in set(_TR.findall(cond_of.get(r, ""))):
            errors.append(f"cycle: '{r}' already gates on '{field}'")
    # forward references — the controller is asked at/after this field
    if field in order:
        fwd = sorted(r for r in (refs & names) if order.get(r, 1 << 30) >= order[field])
        if fwd:
            warnings.append(f"forward reference(s) — asked at/after this field: {', '.join(fwd)}")
    # always-true / always-false probe over sampled assignments of the referenced fields
    if not errors:
        rl = sorted(refs & names)[:6]
        combos, opts = [], ["Yes", "No", "0", "18", "100"]
        for k in range(min(32, 2 ** len(rl) if rl else 1)):
            st_ = {}
            for j, r in enumerate(rl):
                st_[r] = opts[(k >> j) % len(opts)] if len(rl) <= 5 else opts[(k + j) % len(opts)]
            combos.append(st_)
        res = {evaluate_condition(expr, c) for c in combos} if combos else {evaluate_condition(expr, {})}
        if res == {True}:
            warnings.append("always TRUE on sampled inputs — this rule may never hide the field")
        elif res == {False}:
            warnings.append("always FALSE on sampled inputs — this field may never be shown")
    return {"valid": not errors, "errors": errors, "warnings": warnings}


class GraphEditRequest(BaseModel):
    instruction: str                    # natural-language rule change, e.g. "only ask Rx number if prescribed"
    field: str | None = None            # optional explicit target; else the model infers it
    apply: bool = False                 # if valid + apply, write the new condition into the __map__ block


@router.post("/graph/edit")
async def graph_edit(req: GraphEditRequest, scope: Scope = Depends(resolve_scope),
                     db: AsyncSession = Depends(get_db)) -> dict:
    """AI-assisted rule edit: propose a new show-if from a natural-language instruction,
    contradiction-check it, and return a diff. Applies only when valid AND apply=True."""
    from .prompt_blocks import resolve_published_blocks
    from ..bench.llm import client
    from ..config import get_settings

    manifest = await _load_manifest(db, scope)
    if not manifest:
        return {"error": "no form published for this project (missing __map__ block)"}
    fields = [f for s in manifest.get("stages", {}).values() for f in s.get("fields", [])]
    cond_of = {f["name"]: (f.get("cond") or "") for f in fields}

    catalog = "\n".join(f'{f["name"]} | {f.get("cond") or "-"} | {f.get("label") or ""}' for f in fields)
    sysp = (
        "You edit a form's show-if rules. Given the fields (name | current show-if | label) and an instruction, "
        "return the ONE field to change and its NEW show-if condition. Grammar: {FieldName} tokens, isYes({X}), "
        "isNo({X}), values[{X}] <op> value (== != >= <= > <), combined with and/or/not. Reference only existing "
        "fields; never the field itself. Empty string clears the condition. "
        'Reply ONLY JSON: {"field":"<name>","new_condition":"<expr or empty>","rationale":"<short>"}.\n\n' + catalog)
    user = f"INSTRUCTION: {req.instruction}" + (f"\nTARGET FIELD: {req.field}" if req.field else "")
    try:
        r = await client().chat.completions.create(
            model=get_settings().builder_model, temperature=0, response_format={"type": "json_object"},
            messages=[{"role": "system", "content": sysp}, {"role": "user", "content": user}], max_tokens=200)
        prop = json.loads(r.choices[0].message.content or "{}")
    except Exception as e:
        return {"error": f"supervisor model unavailable ({type(e).__name__})"}

    field = prop.get("field") or req.field
    new_cond = (prop.get("new_condition") or "").strip()
    if field not in cond_of:
        return {"error": f"proposed target '{field}' is not a field in this form", "proposal": prop}

    check = _validate_condition(new_cond, field, manifest)
    old_cond = cond_of[field]
    applied = False
    if req.apply and check["valid"] and new_cond != old_cond:
        # write the new condition into the __map__ block and re-publish
        from sqlalchemy import select
        from ..models import PromptBlock
        names = (await db.execute(select(PromptBlock.name).where(
            PromptBlock.tenant_id == scope.tenant_id, PromptBlock.project_id == scope.project_id))).scalars().all()
        mapname = next((n for n in names if n.endswith(".__map__")), None)
        if mapname:
            resolved, _ = await resolve_published_blocks(db, scope, {mapname})
            m = json.loads(resolved.get(mapname, {}).get("content") or "{}")
            for s in m.get("stages", {}).values():
                for f in s.get("fields", []):
                    if f["name"] == field:
                        f["cond"] = new_cond
            from .prompt_blocks import save_block, publish_block, BlockSaveRequest
            try:
                await save_block(BlockSaveRequest(name=mapname, content=json.dumps(m, indent=1), kind="stage"), scope, db)
                await publish_block(mapname, scope, db)
                applied = True
            except Exception:
                applied = False

    return {"field": field, "old_condition": old_cond, "new_condition": new_cond,
            "rationale": prop.get("rationale", ""), "validation": check,
            "diff": {"from": old_cond or "(always shown)", "to": new_cond or "(always shown)"},
            "applied": applied}


class TraceRequest(BaseModel):
    answers: dict = {}                  # FieldName → value (the current submission state)


@router.post("/graph/trace")
async def graph_trace(req: TraceRequest, scope: Scope = Depends(resolve_scope),
                      db: AsyncSession = Depends(get_db)) -> dict:
    """Live execution trace: given an answer set, return each field's runtime state —
    visible (gating), answered, and stale (answered-but-hidden → reconcile would void it) —
    plus the field the driver would ask next. Powers the Studio's live graph coloring."""
    manifest = await _load_manifest(db, scope)
    if not manifest:
        return {"error": "no form published for this project (missing __map__ block)"}
    answers = req.answers or {}
    fields = [f for s in manifest.get("stages", {}).values() for f in s.get("fields", [])]

    out = []
    for f in fields:
        cond = f.get("cond") or ""
        visible = evaluate_condition(cond, answers) if cond else True
        answered = _is_real_answer(answers.get(f["name"]))
        out.append({"name": f["name"], "visible": visible, "answered": answered,
                    "stale": answered and not visible})

    _, nxt = _visible_unanswered(manifest, answers, None, None)
    counts = {
        "answered": sum(1 for x in out if x["answered"] and not x["stale"]),
        "visible_unanswered": sum(1 for x in out if x["visible"] and not x["answered"]),
        "hidden": sum(1 for x in out if not x["visible"] and not x["stale"]),
        "stale": sum(1 for x in out if x["stale"]),
    }
    return {"fields": out, "next_field": (nxt or {}).get("name"), "counts": counts,
            "done": nxt is None}

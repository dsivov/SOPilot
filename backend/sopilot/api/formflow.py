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


def _visible_unanswered(manifest: dict, answers: dict, start_stage: str | None, start_after: str | None):
    """First visible, unanswered field at/after the cursor, across stages in order.
    Returns (stage_id, field) or (None, None) when the form is complete."""
    order = manifest.get("order", [])
    stages = manifest.get("stages", {})
    started = start_stage is None
    passed_cursor = start_after is None
    for sid in order:
        if not started:
            if sid == start_stage:
                started = True
            else:
                continue
        for f in stages.get(sid, {}).get("fields", []):
            if not passed_cursor:
                if f["name"] == start_after:
                    passed_cursor = True
                continue
            if not evaluate_condition(f.get("cond"), answers):        # gating (constraints.py)
                continue
            v = answers.get(f["name"])
            if v is None or str(v).strip() == "":
                return sid, f
    return None, None


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

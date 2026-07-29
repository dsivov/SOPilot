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

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..constraints import evaluate_condition
from ..db import get_db
from ..tenancy import Scope, resolve_scope

router = APIRouter(prefix="/formflow", tags=["formflow"])


class PrepareRequest(BaseModel):
    current_field: str | None = None   # where the user is now (from pt-forms); None = start
    answers: dict = {}                  # answer snapshot (FieldName → value), from pt-forms


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


@router.post("/prepare")
async def prepare(req: PrepareRequest, scope: Scope = Depends(resolve_scope),
                  db: AsyncSession = Depends(get_db)) -> dict:
    from .prompt_blocks import resolve_published_blocks
    from ..bench.llm import client
    from ..config import get_settings

    manifest = await _load_manifest(db, scope)
    if not manifest:
        return {"error": "no form published for this project (missing __map__ block)"}

    cur_stage = _stage_of(manifest, req.current_field)
    stage_id, field = _visible_unanswered(manifest, req.answers or {}, cur_stage, req.current_field)
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
    relevant = {k: v for k, v in (req.answers or {}).items() if k in stage_field_names}

    user = (
        f"CURRENT STAGE: {st['title']}\n\n{playbook}\n\n"
        f"ANSWERS SO FAR (this stage):\n{json.dumps(relevant, ensure_ascii=False) or '(none)'}\n\n"
        f"NEXT FIELD TO ASK: {field['name']} — {field.get('label') or ''}\n\n"
        "Prepare the single compact instruction the realtime agent should speak to ask this field. "
        "Return ONLY the question text (plus a requested-change note if any) — no field id, no metadata."
    )
    try:
        res = await client().chat.completions.create(
            model=get_settings().builder_model,
            messages=[{"role": "system", "content": flow}, {"role": "user", "content": user}],
            temperature=0.2, max_tokens=200)
        instruction = (res.choices[0].message.content or "").strip()
    except Exception as e:
        instruction = field.get("label") or field["name"]  # fallback: raw label
        return {"stage": st["title"], "stage_id": stage_id, "next_field": field["name"],
                "label": field.get("label"), "instruction": instruction,
                "warning": f"supervisor model unavailable ({type(e).__name__}) — raw label used"}

    return {"stage": st["title"], "stage_id": stage_id, "next_field": field["name"],
            "label": field.get("label"), "instruction": instruction, "done": False}

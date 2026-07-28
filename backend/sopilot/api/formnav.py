"""Form navigation endpoint — PolarTie SmartForm pilot (problem #1).

Tenant/project-scoped resolver that PolarTie's forms server (or a test harness)
calls with the form definition, the current answers, where the user is, and a
plain-English request. Relative moves resolve locally; by-meaning requests fall
through to an embedding match over the currently-eligible fields.

Read-from-pt-forms: the caller supplies the answers (fetched from get-fields);
SOPilot never stores them. Field embeddings are cached in-process per form
fingerprint so only the request is embedded each call.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from ..formnav import FormGraph, form_fingerprint, resolve_full
from ..tenancy import Scope, resolve_scope

router = APIRouter(prefix="/formnav", tags=["formnav"])


class NavigateRequest(BaseModel):
    fields: list[dict]                 # the form's fields.json (indexed schema from pt-forms)
    request: str                       # the user's navigation words
    answers: dict = {}                 # current answer snapshot (FieldName → value)
    current_field_id: int | None = None


def _cache(request: Request, fp: str) -> dict:
    store = getattr(request.app.state, "formnav_cache", None)
    if store is None:
        store = request.app.state.formnav_cache = {}
    return store.setdefault(fp, {})


@router.post("/navigate")
async def navigate(req: NavigateRequest, request: Request, scope: Scope = Depends(resolve_scope)) -> dict:
    if not req.request.strip():
        return {"error": "empty request"}
    graph = FormGraph.from_fields_json(req.fields)
    if not graph.fields:
        return {"error": "no fields provided"}
    embed_cache = _cache(request, f"{scope.tenant_id}:{form_fingerprint(graph)}")
    r = await resolve_full(request.app.state.embedder, graph, req.current_field_id,
                           req.answers or {}, req.request, embed_cache=embed_cache)
    return {
        "kind": r.kind, "field_id": r.field_id, "field_name": r.field_name, "label": r.label,
        "confidence": r.confidence, "message": r.message, "candidates": r.candidates,
        "candidate_fields": [
            {"id": graph.by_id[i].id, "name": graph.by_id[i].name, "label": graph.by_id[i].label}
            for i in r.candidates if i in graph.by_id
        ][:8],
    }

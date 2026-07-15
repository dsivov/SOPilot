"""SOP Studio backend: document→draft ingestion and conversational refinement.

Ported from the POC's chat-to-build flow: the model returns the SMALLEST patch
per turn, the server merges it (named lists merged by name, edges deduped,
nodes always re-derived) so the author can watch the graph grow and the model
can never corrupt structure it didn't touch.
"""
from __future__ import annotations

from typing import Any

from .llm import chat_json
from .schemas import TaskDefinition

_SCHEMA_GUIDE = """
TaskDefinition JSON schema (all keys optional in a patch):
- name, description: strings
- user_profile: {name, description, demographics{}}
- conversation_profile: {agent_role, goal, success_markers[], failure_markers[], knowledge}
  (markers are user_state NAMES that end the conversation)
- agent_actions: [{name, description, must_say[], must_not_say[], data_dependencies[], prompt_blocks[]}]
- user_states: [{name, description}]
- cohorts: [{name, description, moods:[{name, description, prior}]}]
- data_dependencies: [{name, description, kind: mock|rag|kg|db|api|mcp, config{},
    expected_latency_ms, cache_ttl_s, idempotent, query_template}]
  (mutating operations MUST set idempotent=false)
- sop: {edges: [{src, dst, direction: forward|backward|both, note}]}
  Edge semantics: action->action forward = hard ordering ("do src before dst");
  state->action forward = trigger ("dst becomes available when the user reaches src").
  NEVER emit sop.nodes — the server derives them.
Names are short CamelCase identifiers (VerifyIdentity, PriceConcern).
"""

INGEST_SYSTEM = (
    "You convert a company's written procedure (policy document, call script, SOP text) into a "
    "structured conversation SOP for a phone/chat agent platform. Extract only what the document "
    "supports — do not invent policy. Return ONE JSON object: a complete TaskDefinition.\n"
    + _SCHEMA_GUIDE
    + "\nGuidelines: 5-12 agent_actions covering the procedure's stages; user_states for the "
    "customer situations the procedure branches on, including at least one success and one "
    "failure terminal (listed in success_markers/failure_markers); ordering edges for mandatory "
    "sequence (e.g. identity verification before account discussion); trigger edges for "
    "conditional stages (objection handling, escalation, polite close); data_dependencies for "
    "every external lookup the document implies (account data, price tables, availability), "
    "kind='mock' unless obvious; a short must_say list where the document mandates wording."
)

BUILDER_SYSTEM = (
    "You are the SOP-building assistant of a conversation-agent platform. The user refines an SOP "
    "conversationally. Each turn: ask at most ONE focused question and return the SMALLEST patch "
    "that applies the user's request. Return ONE JSON object: "
    '{"assistant_message": str, "sop_patch": <partial TaskDefinition or {}>, "is_complete": bool}.\n'
    + _SCHEMA_GUIDE
    + "\nPatch semantics (server-side merge): scalars replace; profile objects shallow-merge; "
    "named lists merge by name (include only changed/new entries); edges are added, duplicates "
    "ignored. To REMOVE a named entry, include it with \"_delete\": true."
)


def _norm_named(entry: Any) -> dict:
    return {"name": entry} if isinstance(entry, str) else dict(entry)


def _merge_named_list(current: list[dict], patch: list[Any]) -> list[dict]:
    by_name = {e["name"]: dict(e) for e in current if isinstance(e, dict) and e.get("name")}
    for raw in patch:
        e = _norm_named(raw)
        name = e.get("name")
        if not name:
            continue
        if e.pop("_delete", False):
            by_name.pop(name, None)
            continue
        if name in by_name:
            by_name[name].update(e)
        else:
            by_name[name] = e
    return list(by_name.values())


def derive_nodes(definition: dict) -> list[str]:
    """nodes = union of action + state names, plus any edge endpoint (so a typo'd
    edge survives merge and gets caught by lint rather than silently dropped)."""
    nodes: list[str] = []
    seen: set[str] = set()
    for coll in ("agent_actions", "user_states"):
        for e in definition.get(coll, []) or []:
            name = e.get("name") if isinstance(e, dict) else e
            if name and name not in seen:
                seen.add(name)
                nodes.append(name)
    for edge in ((definition.get("sop") or {}).get("edges") or []):
        for endpoint in (edge.get("src"), edge.get("dst")):
            if endpoint and endpoint not in seen:
                seen.add(endpoint)
                nodes.append(endpoint)
    return nodes


def _normalize_definition(out: dict) -> None:
    """Forgive predictable LLM shape drift: actions sometimes carry full
    data-dependency objects inline instead of name references — hoist them into
    the top-level catalog and keep the reference. Same for prompt_blocks."""
    deps = [d for d in (out.get("data_dependencies") or []) if isinstance(d, dict) and d.get("name")]
    dep_names = {d["name"] for d in deps}
    for action in out.get("agent_actions") or []:
        if not isinstance(action, dict):
            continue
        fixed_deps: list[str] = []
        for dep in action.get("data_dependencies") or []:
            if isinstance(dep, dict) and dep.get("name"):
                if dep["name"] not in dep_names:
                    deps.append(dict(dep))
                    dep_names.add(dep["name"])
                fixed_deps.append(dep["name"])
            elif isinstance(dep, str):
                fixed_deps.append(dep)
        action["data_dependencies"] = fixed_deps
        action["prompt_blocks"] = [
            b["name"] if isinstance(b, dict) and b.get("name") else b
            for b in (action.get("prompt_blocks") or [])
            if (isinstance(b, dict) and b.get("name")) or isinstance(b, str)
        ]
    if deps:
        out["data_dependencies"] = deps


def merge_patch(current: dict, patch: dict) -> dict:
    out = dict(current)
    for key, value in (patch or {}).items():
        if key in ("agent_actions", "user_states", "cohorts", "data_dependencies"):
            out[key] = _merge_named_list(list(out.get(key) or []), list(value or []))
        elif key in ("user_profile", "conversation_profile"):
            merged = dict(out.get(key) or {})
            merged.update(value or {})
            out[key] = merged
        elif key == "sop":
            sop = dict(out.get("sop") or {})
            existing = list(sop.get("edges") or [])
            seen = {(e.get("src"), e.get("dst"), e.get("direction", "forward")) for e in existing}
            for edge in (value or {}).get("edges") or []:
                sig = (edge.get("src"), edge.get("dst"), edge.get("direction", "forward"))
                if sig not in seen:
                    seen.add(sig)
                    existing.append(dict(edge))
            sop["edges"] = existing
            out["sop"] = sop
        else:
            out[key] = value
    _normalize_definition(out)
    sop = dict(out.get("sop") or {})
    sop["nodes"] = derive_nodes(out)
    out["sop"] = sop
    return out


async def ingest_document(text: str, *, name_hint: str = "") -> TaskDefinition:
    user = (f"Suggested SOP name: {name_hint}\n\n" if name_hint else "") + "PROCEDURE DOCUMENT:\n" + text
    raw = await chat_json(INGEST_SYSTEM, user)
    raw.pop("sop_patch", None)  # in case the model confused the two formats
    merged = merge_patch({}, raw)  # normalizes lists + derives nodes
    return TaskDefinition.model_validate(merged)


async def build_turn(history: list[dict], current_definition: dict) -> tuple[str, dict, bool]:
    """One refinement turn. Returns (assistant_message, updated_definition, is_complete)."""
    import json

    transcript = "\n".join(f"{h.get('role', 'user')}: {h.get('content', '')}" for h in history[-12:])
    user = (
        "CURRENT SOP DEFINITION:\n" + json.dumps(current_definition, indent=1)[:12000]
        + "\n\nCONVERSATION SO FAR:\n" + transcript
    )
    raw = await chat_json(BUILDER_SYSTEM, user)
    message = str(raw.get("assistant_message") or "").strip() or "Done — anything else to adjust?"
    patch = raw.get("sop_patch") or {}
    updated = merge_patch(current_definition, patch)
    TaskDefinition.model_validate(updated)  # raises if the patch corrupted the schema
    return message, updated, bool(raw.get("is_complete"))

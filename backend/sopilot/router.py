"""D-11 SOP router: which procedure should this conversation run?

Measured before built (AENA corpus, 600-opening oracle decomposition):
  - 38% of real openings are not routable yet (greetings) → intake DEFERS;
  - a cheap LLM router agrees with a strong oracle on 88% of routable
    openings — embeddings alone plateau at 63%, so the LLM decides;
  - explicit sop_id from the integration always wins (IVR menu, deep link).

One call at intake per turn until routed; on routed sessions the switch check
runs only when the tracker loses the thread (state repeatedly outside the
current SOP's vocabulary) — the common path costs nothing extra.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

from .bench.llm import chat_json
from .config import get_settings

log = logging.getLogger(__name__)

INTAKE_REPLY = (
    "¡Hola! ¿En qué puedo ayudarle? / Hello! How can I help you?"
)


@dataclass
class RouteDecision:
    sop_id: str | None  # None = defer (stay in intake) or out-of-scope
    kind: str  # initial | defer | oos | switch | keep
    reason: str
    router_ms: int


def _catalog(candidates: list[dict]) -> str:
    return "\n".join(
        f"- id \"{c['id']}\": {c['name']} — {c['description'][:220]}" for c in candidates
    )


ROUTE_SYS = (
    "You route the opening of a service conversation to ONE standard operating procedure, or defer.\n"
    "The text is noisy speech-to-text: expect garbled words, mixed languages, fragments — route on the best "
    "plausible reading, not on perfect clarity.\n"
    "Return JSON {\"route\": \"<procedure id>\" | \"defer\" | \"oos\", \"reason\": \"<max 10 words>\"}.\n"
    "defer = ONLY a pure greeting or truly empty utterance — nothing about the need yet.\n"
    "oos = clearly unrelated to ALL procedures (e.g. medical emergency, job application).\n"
    "When torn between defer/oos and a procedure, PREFER the procedure — a wrong-but-close procedure still helps; "
    "a greeting loop never does."
)

FORCE_SYS = (
    "You MUST route this conversation to exactly ONE of the procedures — defer and oos are not available. "
    "The text is noisy speech-to-text. Pick the closest plausible procedure.\n"
    "Return JSON {\"route\": \"<procedure id>\", \"reason\": \"<max 10 words>\"}."
)

SWITCH_SYS = (
    "A service conversation is running procedure \"{current}\". The customer's latest messages may have moved to a "
    "different topic. Return JSON {{\"route\": \"<procedure id>\" | \"keep\" | \"oos\", \"reason\": \"<max 10 "
    "words>\"}}. Only propose a different procedure if the customer has CLEARLY moved to a need that another "
    "procedure covers better; small digressions and follow-ups stay with \"keep\"."
)


async def route_initial(
    candidates: list[dict], client_utterances: list[str], *, force: bool = False
) -> RouteDecision:
    """force=True (second non-routed turn): intake never loops — pick the
    closest procedure. A wrong-but-close SOP can still answer or hand off;
    a repeated greeting cannot."""
    t0 = time.perf_counter()
    out = await chat_json(
        (FORCE_SYS if force else ROUTE_SYS) + "\nPROCEDURES:\n" + _catalog(candidates),
        [{"role": "user", "content": "Customer so far:\n" + "\n".join(client_utterances[-3:])}],
        model=get_settings().router_model,
    )
    ms = int((time.perf_counter() - t0) * 1000)
    route = str(out.get("route", "defer"))
    reason = str(out.get("reason", ""))[:300]
    known = {c["id"] for c in candidates}
    if route in known:
        return RouteDecision(route, "initial", reason, ms)
    if force:  # model dodged — take the first candidate rather than loop
        return RouteDecision(candidates[0]["id"], "initial", f"forced fallback: {reason}", ms)
    if route == "oos":
        return RouteDecision(None, "oos", reason, ms)
    return RouteDecision(None, "defer", reason, ms)


async def route_switch(
    candidates: list[dict], current_sop_id: str, current_name: str, recent_utterances: list[str]
) -> RouteDecision:
    t0 = time.perf_counter()
    others = [c for c in candidates if c["id"] != current_sop_id]
    if not others:
        return RouteDecision(None, "keep", "no alternative procedures", 0)
    out = await chat_json(
        SWITCH_SYS.format(current=current_name) + "\nPROCEDURES:\n" + _catalog(others),
        [{"role": "user", "content": "Customer's latest messages:\n" + "\n".join(recent_utterances[-2:])}],
        model=get_settings().router_model,
    )
    ms = int((time.perf_counter() - t0) * 1000)
    route = str(out.get("route", "keep"))
    reason = str(out.get("reason", ""))[:300]
    if route in {c["id"] for c in others}:
        return RouteDecision(route, "switch", reason, ms)
    return RouteDecision(None, "keep", reason, ms)

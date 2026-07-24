"""Runtime LLM roles for the TEXT channel: the per-turn classifier/proposer and
the responder. Both run under the critical-path gate (speculative work yields).

Model choice is a config decision with evidence behind it: the research showed a
small classify model collapsing task success (10/10 → 4/10), and our own bench
reproduced it (turn-1 terminal misclassifications). Default is a strong model;
downgrade only with measurements in hand (D-log carry-forward).

The voice channel (P2 next step) replaces `respond` with the realtime speech
model — `classify_and_propose` stays.
"""
from __future__ import annotations

import json
import re

from openai import AsyncOpenAI

from .config import get_settings
from .scheduler import critical_path
from .schemas import TaskDefinition

# Cheap Spanish/English detector for the language rule below. Not a full language
# ID — the deployed languages are ES/EN, and this only needs to name the caller's
# language so a small responder model can be TOLD it (measured: naming the
# language flips a bilingual-context reply from 0/6 to 6/6 correct on gpt-4o-mini;
# a neutral "reply in the caller's language" does not survive the Spanish-heavy
# SOP/fact context). Extend to a real detector when a third language ships.
_ES_MARKERS = re.compile(
    r"[¿¡áéíóúñ]|\b(el|la|los|las|un|una|dónde|cómo|qué|está|estan|están|puede|puedo|"
    r"gracias|hola|equipaje|vuelo|aeropuerto|centro|ciudad|maleta|perdí|necesito|quiero|"
    r"para|con|por|donde|como|mostrador|salida|llegada)\b",
    re.I,
)
_EN_MARKERS = re.compile(
    r"\b(the|my|a|an|where|how|what|is|are|can|could|please|thanks|hello|hi|bag|flight|"
    r"airport|city|lost|need|want|luggage|counter|check|from|here|do|get|arrive|there|"
    r"my|your|which|when|does)\b",
    re.I,
)


def detect_language(text: str, default_iso: str = "") -> str:
    """Return the caller's language NAME ('Spanish' | 'English') for the language
    rule. Ties / no signal fall back to the config's default_language_iso, else
    English."""
    es, en = len(_ES_MARKERS.findall(text)), len(_EN_MARKERS.findall(text))
    if es > en:
        return "Spanish"
    if en > es:
        return "English"
    return "Spanish" if (default_iso or "").lower().startswith("es") else "English"

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        get_settings()  # exports .env into the process env
        _client = AsyncOpenAI()
    return _client


async def classify_and_propose(
    task_def: TaskDefinition,
    history: list[dict],
    user_message: str,
    allowed_actions: list[str],
    *,
    prior_cohort: str = "",
) -> dict:
    """One call: classify (cohort, state, mood) + pick the next SOP-legal action.
    Mirrors the POC's combined classify/propose — one call keeps the live path lean."""
    settings = get_settings()
    cp = task_def.conversation_profile
    terminals = set(cp.success_markers) | set(cp.failure_markers)
    non_terminal = [s.name for s in task_def.user_states if s.name not in terminals]
    states = ", ".join(non_terminal) or "(none declared)"
    terminal_states = ", ".join(sorted(terminals)) or "(none)"
    cohorts = ", ".join(c.name for c in task_def.cohorts) or "(none declared)"
    moods = {c.name: [m.name for m in c.moods] for c in task_def.cohorts if c.moods}
    actions_desc = "\n".join(
        f"- {a.name}: {a.description}" for a in task_def.agent_actions if a.name in allowed_actions
    )
    transcript = "\n".join(f"{h['role']}: {h['content']}" for h in history[-10:])
    system = (
        "You are the conversation supervisor for an agent following a strict procedure. Return JSON: "
        '{"cohort": str, "state": str, "mood": str, "action": str}.\n'
        f"cohort vocabulary: {cohorts}" + (f" (keep prior cohort '{prior_cohort}' unless clearly wrong)\n" if prior_cohort else "\n")
        + f"ordinary user_state vocabulary: {states}\n"
        + f"TERMINAL states that END the conversation: {terminal_states}\n"
        + (f"moods per cohort: {json.dumps(moods)}\n" if moods else "")
        + f"ALLOWED next actions (pick exactly one):\n{actions_desc}\n"
        'Rules: "state" is the ordinary state that best describes the user NOW, or "" when none applies '
        "(most turns: ordinary or empty). Output a TERMINAL state ONLY when the user has EXPLICITLY said "
        "the closing thing itself (confirmed the booking, firmly refused, said goodbye after completion) — "
        "never on a greeting, a request, a question, or partial progress. Pick the action that best "
        "advances the procedure given the user's latest message."
    )
    async with critical_path():
        res = await _get_client().chat.completions.create(
            model=settings.runtime_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": f"Conversation so far:\n{transcript}\n\nUser just said: {user_message}"},
            ],
            temperature=0.2,
            max_tokens=150,
            response_format={"type": "json_object"},
        )
    try:
        out = json.loads(res.choices[0].message.content or "{}")
    except json.JSONDecodeError:
        out = {}
    action = out.get("action") or ""
    if action not in allowed_actions:
        action = allowed_actions[0] if allowed_actions else ""
    state = out.get("state") or ""
    if state and state not in {s.name for s in task_def.user_states}:
        state = ""
    cohort = out.get("cohort") or prior_cohort
    if cohort and task_def.cohorts and cohort not in {c.name for c in task_def.cohorts}:
        cohort = prior_cohort
    return {
        "cohort": cohort,
        "state": state,
        "mood": (out.get("mood") or "").strip()[:40],
        "action": action,
    }


async def pre_generate_reply(
    prompt_text: str,
    history: list[dict],
    predicted_state: str,
    state_description: str = "",
) -> str:
    """Milestone B: draft the agent's next reply BEFORE the user speaks, for a
    predicted (action, state). Runs on the speculative budget (yields to the live
    path); a wrong prediction just ages out of the pool unused.
    """
    from .scheduler import speculative_slot

    settings = get_settings()
    system = (
        "You draft the live agent's NEXT reply in advance. The customer has not spoken yet; the "
        f"prediction is that their next message will express: {predicted_state}"
        + (f" ({state_description})" if state_description else "")
        + ". Write exactly the reply the agent should give in that situation — natural, 1-3 short "
        "sentences, following the instructions below (including any MUST INCLUDE wording). Output "
        "ONLY the reply text.\n\n"
        + (prompt_text or "Respond helpfully and professionally.")
    )
    msgs: list[dict] = [{"role": "system", "content": system}]
    for h in history[-8:]:
        msgs.append({"role": "assistant" if h["role"] == "assistant" else "user", "content": h["content"]})
    async with speculative_slot():
        res = await _get_client().chat.completions.create(
            model=settings.respond_model or settings.runtime_model,
            messages=msgs,
            temperature=0.5,
            max_tokens=200,
        )
    return (res.choices[0].message.content or "").strip()


async def respond(
    prompt_text: str, history: list[dict], user_message: str, *, default_language_iso: str = ""
) -> str:
    """The live agent's reply from the plan-turn instruction payload (text channel)."""
    settings = get_settings()
    in_progress = bool(history)
    # Rule-FIRST with the caller's language NAMED: the SOP/fact text below is
    # bilingual (ES/EN) and a small responder model otherwise echoes the Spanish
    # half to English callers (found on the AENA prod deployment). Position +
    # explicit language both matter — measured 6/6 correct vs 0/6 for a neutral
    # trailing rule on gpt-4o-mini.
    lang = detect_language(user_message, default_language_iso)
    system = (
        f"CRITICAL LANGUAGE RULE — this overrides every instruction below: the caller is writing in {lang}. "
        f"Reply ONLY in {lang}. The reference material below is bilingual (English/Spanish); use only its "
        f"{lang} content and never echo the other language.\n\n"
        "You are the live conversational agent on this channel. Follow the instructions below exactly — "
        "including any MUST INCLUDE wording — and keep replies natural and short (1-3 sentences).\n"
        "CONVERSATION RULES:\n"
        "- If DATA below answers the caller's current question, lead with that answer, concretely — "
        "never replace an available answer with a clarifying question or a referral to screens/staff.\n"
        + ("- The conversation is already in progress: do NOT greet again.\n" if in_progress else "")
        + f"- Stay in {lang}; never ask the caller which language they prefer.\n\n"
        + (prompt_text or "Respond helpfully and professionally.")
    )
    msgs: list[dict] = [{"role": "system", "content": system}]
    for h in history[-10:]:
        msgs.append({"role": "assistant" if h["role"] == "assistant" else "user", "content": h["content"]})
    msgs.append({"role": "user", "content": user_message})
    async with critical_path():
        res = await _get_client().chat.completions.create(
            model=settings.respond_model or settings.runtime_model,
            messages=msgs,
            temperature=0.6,
            max_tokens=220,
        )
    return (res.choices[0].message.content or "").strip()

"""Bench roles: simulated user, state/action proposer, and agent responder.

These mirror the POC's autopilot harness: a "smart human simulator" conditioned
on the SOP's user profile + a sampled cohort/mood, a proposer that classifies the
user state and picks an SOP-legal action, and a responder that plays the live
agent from the plan-turn prompt payload.
"""
from __future__ import annotations

import random

from ..schemas import TaskDefinition
from .llm import chat_json, chat_text


def sample_cohort_and_mood(task_def: TaskDefinition, rng: random.Random) -> tuple[str, str]:
    if not task_def.cohorts:
        return "", ""
    cohort = rng.choice(task_def.cohorts)
    mood = ""
    if cohort.moods:
        weights = [max(0.0, m.prior) for m in cohort.moods]
        mood = rng.choices(cohort.moods, weights=weights, k=1)[0].name
    return cohort.name, mood


async def simulate_user_turn(
    task_def: TaskDefinition,
    cohort: str,
    mood: str,
    history: list[dict],
    persona: str = "",
) -> str:
    system = (
        "You are role-playing a customer on a phone call. Stay fully in character; reply with ONLY the "
        "customer's next utterance (1-2 sentences, natural spoken language).\n"
        f"Customer profile: {task_def.user_profile.description}\n"
        + (f"Persona for this call: {persona}\n" if persona else "")
        + (f"You are of type '{cohort}' feeling '{mood}'. " if cohort or mood else "")
        + f"The call is about: {task_def.description}\n"
        "React realistically to the agent: ask questions, raise concerns fitting your persona, and if "
        "the agent has genuinely addressed your needs, move toward accepting; if handled badly repeatedly, "
        "decline firmly."
    )
    msgs = [
        {"role": "assistant" if h["role"] == "user_sim" else "user", "content": h["text"]}
        for h in history
    ] or [{"role": "user", "content": "(the call connects — you answer the phone)"}]
    return await chat_text(system, msgs)


async def propose(
    task_def: TaskDefinition,
    cohort: str,
    history: list[dict],
    user_message: str,
    allowed_actions: list[str],
    *,
    model: str = "",
) -> dict:
    """Classify the user's state and pick the next agent action from the SOP-legal set."""
    states = ", ".join(s.name for s in task_def.user_states) or "none"
    actions_desc = "\n".join(
        f"- {a.name}: {a.description}" for a in task_def.agent_actions if a.name in allowed_actions
    )
    transcript = "\n".join(f"{h['role']}: {h['text']}" for h in history[-8:])
    system = (
        "You are the conversation supervisor for a phone agent following a procedure. "
        "Return JSON: {\"state\": <one user_state name or empty>, \"mood\": <one word>, "
        "\"action\": <one action name from the allowed list>}.\n"
        f"user_state vocabulary: {states}\n"
        f"ALLOWED next actions (choose exactly one):\n{actions_desc}\n"
        f"Customer type: {cohort}. Pick the action that best advances the procedure given the customer's "
        "latest message; pick the state that best describes the customer NOW. Terminal states "
        "(agreement or firm decline) require an EXPLICIT statement from the customer — never infer them "
        "from a greeting, a question, or hesitation."
    )
    out = await chat_json(
        system,
        [{"role": "user", "content": f"Transcript so far:\n{transcript}\n\nCustomer just said: {user_message}"}],
        model=model,
    )
    action = out.get("action") or ""
    if action not in allowed_actions:
        action = allowed_actions[0] if allowed_actions else ""
    state = out.get("state") or ""
    if state and state not in {s.name for s in task_def.user_states}:
        state = ""
    return {"state": state, "mood": (out.get("mood") or "").strip()[:40], "action": action}


async def respond_as_agent(prompt_text: str, history: list[dict], user_message: str) -> str:
    system = (
        "You are the live phone agent. Follow these instructions exactly; keep the reply to 1-3 spoken "
        "sentences.\n\n" + prompt_text
    )
    msgs = [
        {"role": "assistant" if h["role"] == "agent" else "user", "content": h["text"]}
        for h in history[-8:]
    ]
    msgs.append({"role": "user", "content": user_message})
    return await chat_text(system, msgs)

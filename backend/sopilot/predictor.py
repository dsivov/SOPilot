"""Empirical trajectory predictor — the workhorse (88% recall@3 at ~0 tokens).

Ported from the POC with the research's final verdicts applied:
  - recency decay + shrinkage kept (validated: +12pp cold-start recall@3);
  - Thompson exploration removed (null result: full-information feedback makes
    this online supervised learning, not a bandit);
  - MCTS predictors not ported (retired from the retrieval path).

SQL is Postgres-native and self-joins precedent_traces on (session_id,
turn_index + offset) — the traces are denormalized so no join through turns.
All queries are tenant/project-scoped.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .schemas import TaskDefinition
from .tenancy import Scope


@dataclass
class TrajectoryPrediction:
    action: str
    offset: int  # 1, 2, 3 … turns ahead
    probability: float
    source: str = "empirical"
    predicted_user_state: str | None = None


@dataclass
class PrefetchPlanItem:
    dependency_name: str
    action_name: str
    confidence: float
    predicted_turn_offset: int
    predictor_source: str = "empirical"
    predicted_user_state: str | None = None
    rendered_query: str | None = None


def finalize_probs(
    dist: list[tuple[str, int, float]],
    prior: dict[str, float],
    kappa: float,
) -> list[tuple[str, float]]:
    """Shrinkage toward the SOP-level marginal (Dirichlet-prior smoothing):
        P(a) = (w_a + kappa * prior(a)) / (W + kappa)
    Rich cells are unchanged; sparse cells pull toward the prior. Pure function —
    unit-tested without a database.
    """
    weights = {a: w for a, _, w in dist}
    actions = set(weights) | set(prior)
    if not actions:
        return []
    total = sum(weights.values())
    num = {a: weights.get(a, 0.0) + kappa * prior.get(a, 0.0) for a in actions}
    denom = (total + kappa) or 1.0
    return sorted(((a, num[a] / denom) for a in actions), key=lambda x: -x[1])


class EmpiricalTrajectoryPredictor:
    """P(action at turn N+offset | action at turn N, cohort, mood, state-hint),
    counted over this tenant's precedent traces with success weighting and
    recency decay. Fallback chain per offset:
        cohort+state+mood → cohort+state → cohort → sop-wide.
    """

    NEUTRAL_REWARD = 0.3  # in-progress sessions: neither dominate nor vanish

    def __init__(
        self,
        db: AsyncSession,
        scope: Scope,
        *,
        sop_id: str,
        cohort: str,
        chosen_action: str,
        mood: str | None = None,
        min_supporting: int | None = None,
        recency_half_life_days: float | None = None,
        shrinkage_kappa: float | None = None,
    ):
        settings = get_settings()
        self.db = db
        self.scope = scope
        self.sop_id = sop_id
        self.cohort = cohort
        self.chosen_action = chosen_action
        self.mood = mood or None
        self.min_supporting = min_supporting or settings.predictor_min_supporting
        self.half_life = (
            settings.predictor_recency_half_life_days
            if recency_half_life_days is None
            else recency_half_life_days
        )
        self.kappa = settings.predictor_shrinkage_kappa if shrinkage_kappa is None else max(0.0, shrinkage_kappa)
        self._prior_cache: dict[int, list[tuple[str, int, float]]] = {}

    async def predict(
        self,
        *,
        max_offset: int = 3,
        state_hints: dict[int, list[str]] | None = None,
    ) -> list[TrajectoryPrediction]:
        out: list[TrajectoryPrediction] = []
        for offset in range(1, max_offset + 1):
            hints: list[str | None] = list(state_hints.get(offset, [])) if state_hints else []
            if not hints:
                hints = [None]
            emitted = False
            for hint in hints:
                dist = await self._distribution_with_fallback(offset, hint)
                if sum(c for _, c, _ in dist) < self.min_supporting:
                    continue
                prior = await self._marginal_prior(offset) if self.kappa else {}
                for action, prob in finalize_probs(dist, prior, self.kappa):
                    out.append(
                        TrajectoryPrediction(
                            action=action, offset=offset, probability=prob, predicted_user_state=hint
                        )
                    )
                emitted = True
            if not emitted and any(h is not None for h in hints):
                dist = await self._distribution_with_fallback(offset, None)
                if sum(c for _, c, _ in dist) >= self.min_supporting:
                    prior = await self._marginal_prior(offset) if self.kappa else {}
                    for action, prob in finalize_probs(dist, prior, self.kappa):
                        out.append(TrajectoryPrediction(action=action, offset=offset, probability=prob))
        out.sort(key=lambda p: (p.offset, -p.probability))
        return out

    async def _distribution_with_fallback(
        self, offset: int, state_hint: str | None
    ) -> list[tuple[str, int, float]]:
        if state_hint and self.mood:
            dist = await self._lookup(offset, cohort=True, state_hint=state_hint, use_mood=True)
            if sum(c for _, c, _ in dist) >= self.min_supporting:
                return dist
        if state_hint:
            dist = await self._lookup(offset, cohort=True, state_hint=state_hint, use_mood=False)
            if sum(c for _, c, _ in dist) >= self.min_supporting:
                return dist
            return []
        dist = await self._lookup(offset, cohort=True, state_hint=None, use_mood=False)
        if dist:
            return dist
        return await self._lookup(offset, cohort=False, state_hint=None, use_mood=False)

    async def _lookup(
        self, offset: int, *, cohort: bool, state_hint: str | None, use_mood: bool
    ) -> list[tuple[str, int, float]]:
        params: dict[str, object] = {
            "tenant_id": self.scope.tenant_id,
            "project_id": self.scope.project_id,
            "sop_id": self.sop_id,
            "chosen_action": self.chosen_action,
            "offset": offset,
            "neutral_reward": self.NEUTRAL_REWARD,
        }
        cohort_clause = ""
        if cohort and self.cohort:
            cohort_clause = "AND p.cohort = :cohort"
            params["cohort"] = self.cohort
        state_clause = ""
        if state_hint:
            state_clause = "AND next_p.immediate_state = :state_hint"
            params["state_hint"] = state_hint
        mood_clause = ""
        if use_mood and self.mood:
            mood_clause = "AND p.mood = :mood"
            params["mood"] = self.mood
        if self.half_life and self.half_life > 0:
            params["half_life"] = float(self.half_life)
            decay = "exp(-(EXTRACT(EPOCH FROM (now() - p.created_at)) / 86400.0) / :half_life)"
        else:
            decay = "1.0"
        sql = text(
            f"""
            SELECT next_p.action,
                   COUNT(*) AS freq,
                   SUM(COALESCE(next_p.terminal_reward, :neutral_reward) * {decay}) AS wsum
            FROM precedent_traces p
            JOIN precedent_traces next_p
              ON next_p.session_id = p.session_id
             AND next_p.turn_index = p.turn_index + :offset
            WHERE p.tenant_id = :tenant_id
              AND p.project_id = :project_id
              AND p.sop_id = :sop_id
              AND p.action = :chosen_action
              {cohort_clause}
              {mood_clause}
              {state_clause}
            GROUP BY next_p.action
            ORDER BY wsum DESC
            """
        )
        res = await self.db.execute(sql, params)
        return [(row[0], int(row[1]), float(row[2] or 0.0)) for row in res.all()]

    async def _marginal_prior(self, offset: int) -> dict[str, float]:
        if offset not in self._prior_cache:
            self._prior_cache[offset] = await self._lookup(offset, cohort=False, state_hint=None, use_mood=False)
        dist = self._prior_cache[offset]
        total = sum(w for _, _, w in dist) or 1.0
        return {a: w / total for a, _, w in dist}


def build_prefetch_plan(
    predictions: list[TrajectoryPrediction],
    *,
    task: TaskDefinition,
    decay_lambda: float = 0.3,
    cohort: str = "",
    mood: str = "",
    user_text_override: str | None = None,
) -> list[PrefetchPlanItem]:
    """Predictions → (dependency, offset, action, confidence) plan items.

    Confidence = probability × exp(-decay·offset). When a dependency declares a
    query_template, it renders here; the {user_text} slot takes the cheap-LLM
    predicted utterance when provided (the one validated generative call), else a
    structured stub from (cohort, mood, state, action).
    """
    deps_by_action = {a.name: list(a.data_dependencies or []) for a in task.agent_actions}
    if not any(deps_by_action.values()):
        return []
    dep_by_name = {d.name: d for d in task.data_dependencies}
    scores: dict[tuple[str, int, str], float] = {}
    states: dict[tuple[str, int, str], str | None] = {}
    for pred in predictions:
        for dep_name in deps_by_action.get(pred.action, []):
            key = (dep_name, pred.offset, pred.action)
            scores[key] = scores.get(key, 0.0) + pred.probability * math.exp(-decay_lambda * pred.offset)
            if states.get(key) is None and pred.predicted_user_state:
                states[key] = pred.predicted_user_state
    items: list[PrefetchPlanItem] = []
    for (dep_name, offset, action_name), score in scores.items():
        rendered_query: str | None = None
        dep = dep_by_name.get(dep_name)
        if dep is not None and dep.query_template:
            state_lbl = states.get((dep_name, offset, action_name)) or ""
            user_text = user_text_override or (
                f"a {mood or 'neutral'} customer in cohort {cohort or 'unknown'} "
                f"expected to be in state {state_lbl or 'unknown'}, "
                f"about to receive agent action {action_name}"
            )
            try:
                rendered_query = dep.query_template.format(
                    user_text=user_text, cohort=cohort or "", mood=mood or "",
                    state=state_lbl, action=action_name,
                )
            except (KeyError, IndexError):
                rendered_query = None
        items.append(
            PrefetchPlanItem(
                dependency_name=dep_name,
                action_name=action_name,
                confidence=round(score, 4),
                predicted_turn_offset=offset,
                predicted_user_state=states.get((dep_name, offset, action_name)),
                rendered_query=rendered_query,
            )
        )
    items.sort(key=lambda i: (-i.confidence, i.predicted_turn_offset))
    return items

import math

from sopilot.predictor import PrefetchPlanItem, TrajectoryPrediction, build_prefetch_plan, finalize_probs
from sopilot.schemas import DataDependency, NamedItem, TaskDefinition


def test_finalize_probs_no_shrinkage_is_weight_normalized():
    dist = [("A", 5, 4.0), ("B", 3, 1.0)]
    probs = dict(finalize_probs(dist, prior={}, kappa=0.0))
    assert abs(probs["A"] - 0.8) < 1e-9
    assert abs(probs["B"] - 0.2) < 1e-9


def test_finalize_probs_shrinkage_pulls_sparse_cells_toward_prior():
    dist = [("A", 1, 0.3)]  # sparse cell
    prior = {"A": 0.1, "B": 0.9}
    probs = dict(finalize_probs(dist, prior, kappa=2.0))
    # B has no observed weight but strong prior — it must surface with real mass.
    assert probs["B"] > 0.5
    assert abs(sum(probs.values()) - 1.0) < 1e-9


def test_finalize_probs_rich_cells_barely_move():
    dist = [("A", 100, 90.0), ("B", 10, 10.0)]
    prior = {"A": 0.5, "B": 0.5}
    probs = dict(finalize_probs(dist, prior, kappa=2.0))
    assert abs(probs["A"] - 0.9) < 0.01


def make_task() -> TaskDefinition:
    return TaskDefinition(
        name="t",
        agent_actions=[
            NamedItem(name="PitchRenewal", data_dependencies=["policy"]),
            NamedItem(name="HandleObjection", data_dependencies=["market_rates"]),
            NamedItem(name="Greeting"),
        ],
        data_dependencies=[
            DataDependency(name="policy", kind="mock"),
            DataDependency(
                name="market_rates",
                kind="rag",
                query_template="rates for {cohort} customer: {user_text}",
            ),
        ],
    )


def test_build_prefetch_plan_confidence_decay_and_ordering():
    preds = [
        TrajectoryPrediction(action="PitchRenewal", offset=1, probability=0.9),
        TrajectoryPrediction(action="PitchRenewal", offset=3, probability=0.9),
        TrajectoryPrediction(action="Greeting", offset=1, probability=1.0),  # no deps → no plan item
    ]
    plan = build_prefetch_plan(preds, task=make_task(), decay_lambda=0.3)
    assert all(isinstance(i, PrefetchPlanItem) for i in plan)
    assert [i.predicted_turn_offset for i in plan] == [1, 3]  # closer offset first (higher confidence)
    assert abs(plan[0].confidence - 0.9 * math.exp(-0.3)) < 1e-3
    assert plan[0].confidence > plan[1].confidence


def test_build_prefetch_plan_renders_query_template():
    preds = [
        TrajectoryPrediction(
            action="HandleObjection", offset=1, probability=0.8, predicted_user_state="Objecting"
        )
    ]
    plan = build_prefetch_plan(
        preds, task=make_task(), cohort="PriceShopper", mood="irritated",
        user_text_override="why is my premium going up?",
    )
    assert len(plan) == 1
    assert plan[0].rendered_query == "rates for PriceShopper customer: why is my premium going up?"


def test_build_prefetch_plan_stub_when_no_user_text():
    preds = [TrajectoryPrediction(action="HandleObjection", offset=2, probability=0.5)]
    plan = build_prefetch_plan(preds, task=make_task(), cohort="Loyal", mood="calm")
    assert "Loyal" in plan[0].rendered_query and "HandleObjection" in plan[0].rendered_query

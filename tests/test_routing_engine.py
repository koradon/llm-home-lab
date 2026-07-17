import pytest

from llm_home_lab.api.models import ChatCompletionRequest, Message
from llm_home_lab.routing.engine import RoutingEngine
from llm_home_lab.routing.models import (
    NoAvailableBackendError,
    PolicyRule,
    RoutingCandidate,
    RoutingPolicy,
)


class FakeBackend:
    def __init__(self, backend_id: str) -> None:
        self.backend_id = backend_id


def _request(**overrides: object) -> ChatCompletionRequest:
    defaults: dict[str, object] = {
        "model": "test-model",
        "messages": [Message(role="user", content="hi")],
    }
    defaults.update(overrides)
    return ChatCompletionRequest(**defaults)


def _candidate(
    backend_id: str, latency_ms: float = 0.0, context_window: int = 8192
) -> RoutingCandidate:
    return RoutingCandidate(
        backend=FakeBackend(backend_id),
        latency_ms=latency_ms,
        context_window=context_window,
    )


def _lower_latency_wins_policy() -> RoutingPolicy:
    return RoutingPolicy(
        rules=[
            PolicyRule(
                name="prefer-lower-latency", score_fn=lambda candidate, ctx: -candidate.latency_ms
            )
        ]
    )


def test_latency_preferring_rule_selects_the_fastest_healthy_candidate():
    policy = _lower_latency_wins_policy()
    engine = RoutingEngine(policy)
    candidates = [_candidate("slow", latency_ms=200.0), _candidate("fast", latency_ms=20.0)]

    decision = engine.select_backend(_request(), candidates)

    assert decision.backend_id == "fast"


def test_task_type_rule_contributes_score_for_a_matching_request():
    policy = RoutingPolicy(
        rules=[PolicyRule(name="prefer-code", task_type="code", score_fn=lambda c, ctx: 10.0)]
    )
    engine = RoutingEngine(policy)
    candidates = [_candidate("only-candidate")]

    decision = engine.select_backend(_request(task_type="code"), candidates)

    assert decision.scores["only-candidate"] == 10.0
    assert "prefer-code" in decision.matched_rules["only-candidate"]


def test_task_type_rule_does_not_contribute_for_a_non_matching_request():
    policy = RoutingPolicy(
        rules=[PolicyRule(name="prefer-code", task_type="code", score_fn=lambda c, ctx: 10.0)]
    )
    engine = RoutingEngine(policy)
    candidates = [_candidate("only-candidate")]

    decision = engine.select_backend(_request(task_type="general"), candidates)

    assert decision.scores["only-candidate"] == 0.0
    assert decision.matched_rules["only-candidate"] == []


def test_token_budget_excludes_undersized_backends():
    policy = RoutingPolicy(
        rules=[
            PolicyRule(
                name="always-prefer-undersized",
                score_fn=lambda c, ctx: 100.0 if c.backend.backend_id == "undersized" else 0.0,
            )
        ]
    )
    engine = RoutingEngine(policy)
    long_message = Message(role="user", content="x" * 40)
    candidates = [
        _candidate("undersized", context_window=5),
        _candidate("adequate", context_window=8192),
    ]

    decision = engine.select_backend(_request(messages=[long_message]), candidates)

    assert decision.backend_id == "adequate"
    assert "undersized" not in decision.scores


def test_no_eligible_candidates_raises_no_available_backend_error():
    policy = _lower_latency_wins_policy()
    engine = RoutingEngine(policy)
    long_message = Message(role="user", content="x" * 40)
    candidates = [_candidate("undersized", context_window=5)]

    with pytest.raises(NoAvailableBackendError):
        engine.select_backend(_request(messages=[long_message]), candidates)


def test_routing_decisions_are_reproducible_for_identical_inputs():
    policy = _lower_latency_wins_policy()
    engine = RoutingEngine(policy)
    candidates = [_candidate("a", latency_ms=50.0), _candidate("b", latency_ms=10.0)]
    request = _request()

    first = engine.select_backend(request, candidates)
    second = engine.select_backend(request, candidates)

    assert first == second


def test_equal_scores_break_ties_by_backend_id_not_iteration_order():
    policy = RoutingPolicy(rules=[PolicyRule(name="flat", score_fn=lambda c, ctx: 1.0)])
    engine = RoutingEngine(policy)

    decision_ordered_z_then_a = engine.select_backend(
        _request(), [_candidate("zebra"), _candidate("apple")]
    )
    decision_ordered_a_then_z = engine.select_backend(
        _request(), [_candidate("apple"), _candidate("zebra")]
    )

    assert decision_ordered_z_then_a.backend_id == "apple"
    assert decision_ordered_a_then_z.backend_id == "apple"


def test_sticky_routing_records_the_winning_backend_on_first_turn():
    policy = _lower_latency_wins_policy()
    engine = RoutingEngine(policy)
    candidates = [_candidate("slow", latency_ms=200.0), _candidate("fast", latency_ms=20.0)]

    decision = engine.select_backend(_request(), candidates, session_id="session-1")

    assert decision.backend_id == "fast"
    assert engine.sticky_backend_for("session-1") == "fast"


def test_sticky_routing_reuses_the_recorded_backend_on_later_turns_without_rescoring():
    policy = _lower_latency_wins_policy()
    engine = RoutingEngine(policy)
    first_turn_candidates = [
        _candidate("slow", latency_ms=200.0),
        _candidate("fast", latency_ms=20.0),
    ]
    engine.select_backend(_request(), first_turn_candidates, session_id="session-1")

    # On the next turn "slow" would now win on latency alone, but stickiness should override that.
    later_turn_candidates = [
        _candidate("slow", latency_ms=1.0),
        _candidate("fast", latency_ms=999.0),
    ]
    decision = engine.select_backend(_request(), later_turn_candidates, session_id="session-1")

    assert decision.backend_id == "fast"


def test_sticky_routing_falls_back_when_recorded_backend_is_no_longer_a_candidate():
    policy = _lower_latency_wins_policy()
    engine = RoutingEngine(policy)
    engine.select_backend(
        _request(),
        [_candidate("slow", latency_ms=200.0), _candidate("fast", latency_ms=20.0)],
        session_id="session-1",
    )

    remaining_candidates = [_candidate("slow", latency_ms=200.0)]
    decision = engine.select_backend(_request(), remaining_candidates, session_id="session-1")

    assert decision.backend_id == "slow"
    # The sticky record is left alone for "fast" to recover, not overwritten to "slow".
    assert engine.sticky_backend_for("session-1") == "fast"


def test_sticky_routing_can_be_disabled():
    policy = _lower_latency_wins_policy()
    engine = RoutingEngine(policy, sticky_sessions_enabled=False)
    engine.select_backend(
        _request(),
        [_candidate("slow", latency_ms=200.0), _candidate("fast", latency_ms=20.0)],
        session_id="session-1",
    )

    later_turn_candidates = [
        _candidate("slow", latency_ms=1.0),
        _candidate("fast", latency_ms=999.0),
    ]
    decision = engine.select_backend(_request(), later_turn_candidates, session_id="session-1")

    assert decision.backend_id == "slow"


def test_routing_without_a_session_id_never_applies_sticky_logic():
    policy = _lower_latency_wins_policy()
    engine = RoutingEngine(policy)
    engine.select_backend(
        _request(), [_candidate("slow", latency_ms=200.0), _candidate("fast", latency_ms=20.0)]
    )

    later_turn_candidates = [
        _candidate("slow", latency_ms=1.0),
        _candidate("fast", latency_ms=999.0),
    ]
    decision = engine.select_backend(_request(), later_turn_candidates)

    assert decision.backend_id == "slow"

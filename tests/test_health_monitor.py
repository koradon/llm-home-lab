from datetime import UTC, datetime, timedelta

from llm_home_lab.api.models import ChatCompletionRequest, Message
from llm_home_lab.health.models import FailoverEvent
from llm_home_lab.health.monitor import HealthMonitor
from llm_home_lab.routing.engine import RoutingEngine
from llm_home_lab.routing.models import PolicyRule, RoutingCandidate, RoutingPolicy

T0 = datetime(2026, 1, 1, tzinfo=UTC)


class FakeBackend:
    def __init__(self, backend_id: str) -> None:
        self.backend_id = backend_id


def _candidate(backend_id: str, latency_ms: float = 0.0) -> RoutingCandidate:
    return RoutingCandidate(
        backend=FakeBackend(backend_id), latency_ms=latency_ms, context_window=8192
    )


def test_a_backend_with_no_probes_is_healthy_by_default():
    monitor = HealthMonitor()

    assert monitor.is_healthy("backend-a", T0) is True


def test_a_backend_with_no_probes_has_no_probe_history():
    monitor = HealthMonitor()

    assert monitor.has_probe_history("backend-a") is False


def test_a_backend_with_a_recorded_probe_has_probe_history():
    monitor = HealthMonitor()

    monitor.record_probe("backend-a", healthy=True, at=T0)

    assert monitor.has_probe_history("backend-a") is True


def test_fewer_than_failure_threshold_stays_healthy():
    monitor = HealthMonitor(failure_threshold=3)

    monitor.record_probe("backend-a", healthy=False, at=T0)
    monitor.record_probe("backend-a", healthy=False, at=T0 + timedelta(seconds=1))

    assert monitor.is_healthy("backend-a", T0 + timedelta(seconds=1)) is True


def test_crossing_failure_threshold_triggers_failover():
    monitor = HealthMonitor(failure_threshold=3)

    monitor.record_probe("backend-a", healthy=False, at=T0)
    monitor.record_probe("backend-a", healthy=False, at=T0 + timedelta(seconds=1))
    monitor.record_probe("backend-a", healthy=False, at=T0 + timedelta(seconds=2))

    assert monitor.is_healthy("backend-a", T0 + timedelta(seconds=2)) is False
    assert monitor.events == [
        FailoverEvent(
            backend_id="backend-a",
            from_healthy=True,
            to_healthy=False,
            at=T0 + timedelta(seconds=2),
        )
    ]


def test_unhealthy_backend_stays_excluded_for_the_full_cooldown_even_with_a_success_recorded():
    monitor = HealthMonitor(failure_threshold=1, cooldown=timedelta(seconds=30))
    monitor.record_probe("backend-a", healthy=False, at=T0)

    monitor.record_probe("backend-a", healthy=True, at=T0 + timedelta(seconds=1))

    assert monitor.is_healthy("backend-a", T0 + timedelta(seconds=1)) is False
    assert monitor.is_healthy("backend-a", T0 + timedelta(seconds=29)) is False


def test_recovery_requires_consecutive_successes_after_cooldown():
    monitor = HealthMonitor(
        failure_threshold=1, recovery_threshold=2, cooldown=timedelta(seconds=30)
    )
    monitor.record_probe("backend-a", healthy=False, at=T0)

    monitor.record_probe("backend-a", healthy=True, at=T0 + timedelta(seconds=31))

    assert monitor.is_healthy("backend-a", T0 + timedelta(seconds=31)) is False


def test_crossing_recovery_threshold_after_cooldown_ends_the_failover():
    monitor = HealthMonitor(
        failure_threshold=1, recovery_threshold=2, cooldown=timedelta(seconds=30)
    )
    monitor.record_probe("backend-a", healthy=False, at=T0)
    monitor.record_probe("backend-a", healthy=True, at=T0 + timedelta(seconds=31))

    monitor.record_probe("backend-a", healthy=True, at=T0 + timedelta(seconds=32))

    assert monitor.is_healthy("backend-a", T0 + timedelta(seconds=32)) is True
    assert monitor.events[-1] == FailoverEvent(
        backend_id="backend-a",
        from_healthy=False,
        to_healthy=True,
        at=T0 + timedelta(seconds=32),
    )


def test_failure_during_recovery_phase_resets_progress_and_restarts_cooldown():
    monitor = HealthMonitor(
        failure_threshold=1, recovery_threshold=2, cooldown=timedelta(seconds=30)
    )
    monitor.record_probe("backend-a", healthy=False, at=T0)
    # One success after cooldown starts the recovery count, then a failure resets it.
    monitor.record_probe("backend-a", healthy=True, at=T0 + timedelta(seconds=31))
    monitor.record_probe("backend-a", healthy=False, at=T0 + timedelta(seconds=32))

    # Two successes right after the failure should NOT be enough: cooldown restarted at t=32.
    monitor.record_probe("backend-a", healthy=True, at=T0 + timedelta(seconds=33))
    monitor.record_probe("backend-a", healthy=True, at=T0 + timedelta(seconds=34))

    assert monitor.is_healthy("backend-a", T0 + timedelta(seconds=34)) is False


def test_health_score_reflects_recent_probe_history_independent_of_exclusion_state():
    monitor = HealthMonitor(failure_threshold=1, cooldown=timedelta(seconds=30))
    monitor.record_probe("backend-a", healthy=False, at=T0)

    monitor.record_probe("backend-a", healthy=True, at=T0 + timedelta(seconds=1))
    monitor.record_probe("backend-a", healthy=True, at=T0 + timedelta(seconds=2))

    assert monitor.is_healthy("backend-a", T0 + timedelta(seconds=2)) is False
    assert monitor.health_score("backend-a") == 2 / 3


def test_health_score_is_one_when_no_probes_recorded():
    monitor = HealthMonitor()

    assert monitor.health_score("backend-a") == 1.0


def test_health_filtered_candidates_let_routing_fall_back_from_an_unhealthy_sticky_backend():
    monitor = HealthMonitor(failure_threshold=1)
    policy = RoutingPolicy(rules=[PolicyRule(name="flat", score_fn=lambda c, ctx: -c.latency_ms)])
    router = RoutingEngine(policy)
    all_candidates = [_candidate("fast", latency_ms=1.0), _candidate("slow", latency_ms=100.0)]
    request = ChatCompletionRequest(
        model="test-model", messages=[Message(role="user", content="hi")]
    )
    router.select_backend(request, all_candidates, session_id="session-1")
    assert router.sticky_backend_for("session-1") == "fast"

    monitor.record_probe("fast", healthy=False, at=T0)
    healthy_candidates = [c for c in all_candidates if monitor.is_healthy(c.backend.backend_id, T0)]
    decision = router.select_backend(request, healthy_candidates, session_id="session-1")

    assert "fast" not in [c.backend.backend_id for c in healthy_candidates]
    assert decision.backend_id == "slow"
    assert router.sticky_backend_for("session-1") == "fast"

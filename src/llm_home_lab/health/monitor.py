import logging
from datetime import datetime, timedelta

from llm_home_lab.health.models import BackendHealthState, FailoverEvent

health_logger = logging.getLogger("llm_home_lab.health")


class HealthMonitor:
    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_threshold: int = 2,
        cooldown: timedelta = timedelta(seconds=30),
    ) -> None:
        self._failure_threshold = failure_threshold
        self._recovery_threshold = recovery_threshold
        self._cooldown = cooldown
        self._states: dict[str, BackendHealthState] = {}
        self._events: list[FailoverEvent] = []

    @property
    def events(self) -> list[FailoverEvent]:
        return list(self._events)

    def is_healthy(self, backend_id: str, at: datetime) -> bool:
        return self._states.get(backend_id, BackendHealthState()).is_healthy

    def health_score(self, backend_id: str) -> float:
        state = self._states.get(backend_id)
        if state is None or not state.history:
            return 1.0
        return sum(state.history) / len(state.history)

    def record_probe(self, backend_id: str, healthy: bool, at: datetime) -> None:
        state = self._states.setdefault(backend_id, BackendHealthState())
        state.history.append(healthy)

        if not state.is_healthy:
            self._update_unhealthy_state(state, backend_id, healthy, at)
            return

        if healthy:
            state.consecutive_failures = 0
            return

        state.consecutive_failures += 1
        if state.consecutive_failures >= self._failure_threshold:
            state.is_healthy = False
            state.unhealthy_since = at
            state.consecutive_successes = 0
            self._record_transition(backend_id, from_healthy=True, to_healthy=False, at=at)

    def _update_unhealthy_state(
        self, state: BackendHealthState, backend_id: str, healthy: bool, at: datetime
    ) -> None:
        if not healthy:
            state.consecutive_successes = 0
            state.unhealthy_since = at
            return

        unhealthy_since = state.unhealthy_since
        if unhealthy_since is None or at < unhealthy_since + self._cooldown:
            return

        state.consecutive_successes += 1
        if state.consecutive_successes >= self._recovery_threshold:
            state.is_healthy = True
            state.consecutive_failures = 0
            state.consecutive_successes = 0
            state.unhealthy_since = None
            self._record_transition(backend_id, from_healthy=False, to_healthy=True, at=at)

    def _record_transition(
        self, backend_id: str, *, from_healthy: bool, to_healthy: bool, at: datetime
    ) -> None:
        event = FailoverEvent(
            backend_id=backend_id, from_healthy=from_healthy, to_healthy=to_healthy, at=at
        )
        self._events.append(event)
        health_logger.info(
            "backend_id=%s from_healthy=%s to_healthy=%s at=%s",
            backend_id,
            from_healthy,
            to_healthy,
            at.isoformat(),
        )

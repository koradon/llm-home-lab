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
        self._model_states: dict[tuple[str, str], BackendHealthState] = {}
        self._events: list[FailoverEvent] = []

    @property
    def events(self) -> list[FailoverEvent]:
        return list(self._events)

    def is_healthy(self, backend_id: str, at: datetime) -> bool:
        return self._states.get(backend_id, BackendHealthState()).is_healthy

    def has_probe_history(self, backend_id: str) -> bool:
        return backend_id in self._states

    def health_score(self, backend_id: str) -> float:
        state = self._states.get(backend_id)
        if state is None or not state.history:
            return 1.0
        return sum(state.history) / len(state.history)

    def failure_count(self, backend_id: str) -> int:
        state = self._states.get(backend_id)
        if state is None:
            return 0
        return sum(1 for healthy in state.history if not healthy)

    def is_model_healthy(self, backend_id: str, model: str, at: datetime) -> bool:
        return self._model_states.get((backend_id, model), BackendHealthState()).is_healthy

    def may_admit_model(self, backend_id: str, model: str, at: datetime) -> bool:
        state = self._model_states.get((backend_id, model))
        if state is None or state.is_healthy:
            return True
        if state.trial_in_flight:
            return False
        unhealthy_since = state.unhealthy_since
        return unhealthy_since is not None and at >= unhealthy_since + self._cooldown

    def has_model_trial(self, backend_id: str, model: str) -> bool:
        state = self._model_states.get((backend_id, model))
        return state is not None and state.trial_in_flight

    def claim_model_trial(self, backend_id: str, model: str, at: datetime) -> bool:
        state = self._model_states.get((backend_id, model))
        if state is None or state.is_healthy:
            return True
        if state.trial_in_flight:
            return False
        unhealthy_since = state.unhealthy_since
        if unhealthy_since is None or at < unhealthy_since + self._cooldown:
            return False
        state.trial_in_flight = True
        return True

    def release_model_trial(self, backend_id: str, model: str) -> None:
        state = self._model_states.get((backend_id, model))
        if state is not None:
            state.trial_in_flight = False

    def model_failure_count(self, backend_id: str, model: str) -> int:
        state = self._model_states.get((backend_id, model))
        if state is None:
            return 0
        return sum(1 for healthy in state.history if not healthy)

    def tracked_models(self, backend_id: str) -> list[str]:
        return [model for host, model in self._model_states if host == backend_id]

    def record_probe(self, backend_id: str, healthy: bool, at: datetime) -> None:
        state = self._states.setdefault(backend_id, BackendHealthState())
        self._apply_probe(state, backend_id, healthy, at)

    def record_model_probe(self, backend_id: str, model: str, healthy: bool, at: datetime) -> None:
        state = self._model_states.setdefault((backend_id, model), BackendHealthState())
        self._apply_probe(state, backend_id, healthy, at, model=model)

    def _apply_probe(
        self,
        state: BackendHealthState,
        backend_id: str,
        healthy: bool,
        at: datetime,
        *,
        model: str | None = None,
    ) -> None:
        state.history.append(healthy)

        if not state.is_healthy:
            self._update_unhealthy_state(state, backend_id, healthy, at, model=model)
            return

        state.trial_in_flight = False
        if healthy:
            state.consecutive_failures = 0
            return

        state.consecutive_failures += 1
        if state.consecutive_failures >= self._failure_threshold:
            state.is_healthy = False
            state.unhealthy_since = at
            state.consecutive_successes = 0
            self._record_transition(
                backend_id, from_healthy=True, to_healthy=False, at=at, model=model
            )

    def _update_unhealthy_state(
        self,
        state: BackendHealthState,
        backend_id: str,
        healthy: bool,
        at: datetime,
        *,
        model: str | None = None,
    ) -> None:
        was_trial = state.trial_in_flight
        state.trial_in_flight = False

        if not healthy:
            state.consecutive_successes = 0
            state.unhealthy_since = at
            return

        unhealthy_since = state.unhealthy_since
        if unhealthy_since is None or at < unhealthy_since + self._cooldown:
            return

        if was_trial:
            state.is_healthy = True
            state.consecutive_failures = 0
            state.consecutive_successes = 0
            state.unhealthy_since = None
            self._record_transition(
                backend_id, from_healthy=False, to_healthy=True, at=at, model=model
            )
            return

        state.consecutive_successes += 1
        if state.consecutive_successes >= self._recovery_threshold:
            state.is_healthy = True
            state.consecutive_failures = 0
            state.consecutive_successes = 0
            state.unhealthy_since = None
            self._record_transition(
                backend_id, from_healthy=False, to_healthy=True, at=at, model=model
            )

    def _record_transition(
        self,
        backend_id: str,
        *,
        from_healthy: bool,
        to_healthy: bool,
        at: datetime,
        model: str | None = None,
    ) -> None:
        event = FailoverEvent(
            backend_id=backend_id,
            from_healthy=from_healthy,
            to_healthy=to_healthy,
            at=at,
            model=model,
        )
        self._events.append(event)
        if model is None:
            health_logger.info(
                "backend_id=%s from_healthy=%s to_healthy=%s at=%s",
                backend_id,
                from_healthy,
                to_healthy,
                at.isoformat(),
            )
        else:
            health_logger.info(
                "backend_id=%s model=%s from_healthy=%s to_healthy=%s at=%s",
                backend_id,
                model,
                from_healthy,
                to_healthy,
                at.isoformat(),
            )

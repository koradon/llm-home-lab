import time
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from registry_test_helpers import inert_external_load_probe, new_registry_db_path

from llm_home_lab.api.app import create_app
from llm_home_lab.backends.base import BackendHealth
from llm_home_lab.health.monitor import HealthMonitor
from llm_home_lab.observability.alerts import AlertEvaluator
from llm_home_lab.observability.metrics import MetricsRegistry
from llm_home_lab.registry.models import HostCapabilities, HostCapacity
from llm_home_lab.registry.registry import HostRegistry
from llm_home_lab.routing.engine import RoutingEngine
from llm_home_lab.routing.models import PolicyRule, RoutingPolicy
from llm_home_lab.scheduling.queue import SchedulingQueue
from llm_home_lab.security.key_store import ApiKeyStore
from llm_home_lab.security.models import ApiKey, ClientConfig


def _permissive_key_store() -> ApiKeyStore:
    return ApiKeyStore(
        [
            ClientConfig(
                client_id="test-client",
                allowed_path_prefixes=["/"],
                keys=[ApiKey(key="test-key", expires_at=None)],
            )
        ]
    )


class SwitchableBackend:
    backend_id = "switchable"

    def __init__(self, healthy: bool = True):
        self.healthy = healthy
        self.probe_count = 0

    async def check_health(self):
        self.probe_count += 1
        return BackendHealth(healthy=self.healthy, detail="ok" if self.healthy else "down")


class FlakyBackend:
    backend_id = "flaky"

    def __init__(self):
        self.probe_count = 0

    async def check_health(self):
        self.probe_count += 1
        raise RuntimeError("boom")


def _app_for(backend, health_monitor, health_poll_interval=None):
    registry = HostRegistry(new_registry_db_path())
    registry.register(
        backend.backend_id,
        HostCapabilities(backend_type=backend.backend_id, context_window=8192, base_url="unused"),
        HostCapacity(max_concurrent_requests=1000),
        at=datetime.now(UTC),
    )
    policy = RoutingPolicy(rules=[PolicyRule(name="flat", score_fn=lambda c, ctx: 0.0)])
    return create_app(
        registry=registry,
        router=RoutingEngine(policy),
        health_monitor=health_monitor,
        scheduling_queue=SchedulingQueue(),
        backend_factories={backend.backend_id: (lambda b: lambda caps: b)(backend)},
        metrics_registry=MetricsRegistry(),
        alert_evaluator=AlertEvaluator([]),
        key_store=_permissive_key_store(),
        external_load_probe=inert_external_load_probe(),
        health_poll_interval=health_poll_interval,
    )


def test_background_poller_recovers_health_without_any_client_call():
    """Regression test for the incident this plan fixes: a host recovers even though
    nothing ever calls /health/ready — only the background poller does.
    """
    backend = SwitchableBackend(healthy=True)
    monitor = HealthMonitor(
        failure_threshold=1, recovery_threshold=1, cooldown=timedelta(seconds=0)
    )
    # Seed an unhealthy state from "before", as if a real outage was already recorded.
    monitor.record_probe(backend.backend_id, healthy=False, at=datetime.now(UTC))
    assert monitor.is_healthy(backend.backend_id, datetime.now(UTC)) is False

    app = _app_for(backend, monitor, health_poll_interval=0.01)

    with TestClient(app):
        time.sleep(0.2)  # several poll ticks, no request ever sent through the client

    assert monitor.is_healthy(backend.backend_id, datetime.now(UTC)) is True
    assert backend.probe_count > 1


def test_poller_survives_a_raising_check_health():
    backend = FlakyBackend()
    monitor = HealthMonitor()
    app = _app_for(backend, monitor, health_poll_interval=0.01)

    with TestClient(app):
        time.sleep(0.2)

    # The loop kept retrying after the exception instead of dying on the first tick.
    assert backend.probe_count > 1


def test_no_poll_interval_means_no_background_task():
    backend = SwitchableBackend(healthy=True)
    monitor = HealthMonitor(
        failure_threshold=1, recovery_threshold=1, cooldown=timedelta(seconds=0)
    )
    monitor.record_probe(backend.backend_id, healthy=False, at=datetime.now(UTC))
    app = _app_for(backend, monitor, health_poll_interval=None)

    with TestClient(app):
        time.sleep(0.2)

    # Nothing probed it — no client request, no background task — so it stays exactly
    # where it was seeded: unhealthy.
    assert backend.probe_count == 0
    assert monitor.is_healthy(backend.backend_id, datetime.now(UTC)) is False


def test_poller_stops_after_lifespan_shutdown():
    backend = SwitchableBackend(healthy=True)
    monitor = HealthMonitor()
    app = _app_for(backend, monitor, health_poll_interval=0.01)

    with TestClient(app):
        time.sleep(0.1)

    count_at_shutdown = backend.probe_count
    time.sleep(0.1)

    assert backend.probe_count == count_at_shutdown  # no more ticks after the context exits

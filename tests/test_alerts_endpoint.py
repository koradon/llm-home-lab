from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from registry_test_helpers import inert_external_load_probe, new_registry_db_path

from llm_home_lab.api.app import create_app
from llm_home_lab.backends.base import BackendHealth
from llm_home_lab.health.monitor import HealthMonitor
from llm_home_lab.observability.alerts import AlertEvaluator
from llm_home_lab.observability.metrics import MetricsRegistry
from llm_home_lab.observability.models import AlertRule, AlertSeverity, SliSnapshot
from llm_home_lab.registry.models import HostCapabilities, HostCapacity
from llm_home_lab.registry.registry import HostRegistry
from llm_home_lab.routing.engine import RoutingEngine
from llm_home_lab.routing.models import PolicyRule, RoutingPolicy
from llm_home_lab.scheduling.queue import SchedulingQueue
from llm_home_lab.security.key_store import ApiKeyStore
from llm_home_lab.security.models import ApiKey, ClientConfig

AUTH_HEADERS = {"Authorization": "Bearer test-key"}


class HealthyFakeBackend:
    backend_id = "host-a"

    async def check_health(self):
        return BackendHealth(healthy=True, detail="ok")


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


def _app_for(alert_evaluator=None, registry=None):
    registry = registry or HostRegistry(new_registry_db_path())
    if not registry.hosts():
        registry.register(
            "host-a",
            HostCapabilities(backend_type="fake", context_window=8192, base_url="unused"),
            HostCapacity(max_concurrent_requests=4),
            at=datetime.now(UTC),
        )
    policy = RoutingPolicy(rules=[PolicyRule(name="flat", score_fn=lambda c, ctx: 0.0)])
    return create_app(
        registry=registry,
        router=RoutingEngine(policy),
        health_monitor=HealthMonitor(),
        scheduling_queue=SchedulingQueue(),
        backend_factories={"fake": lambda caps: HealthyFakeBackend()},
        key_store=_permissive_key_store(),
        metrics_registry=MetricsRegistry(),
        alert_evaluator=alert_evaluator or AlertEvaluator([]),
        external_load_probe=inert_external_load_probe(),
    )


def test_alerts_endpoint_requires_authentication():
    client = TestClient(_app_for())

    response = client.get("/v1/alerts")

    assert response.status_code == 401


def test_alerts_endpoint_lists_currently_firing_alerts():
    rule = AlertRule(
        name="p95-latency-threshold",
        kind="threshold",
        metric="p95_latency_ms",
        comparison="gt",
        threshold_value=5000.0,
        window=timedelta(minutes=5),
        severity=AlertSeverity.WARNING,
        runbook_url="docs/runbooks/p95-latency-threshold.md",
    )
    evaluator = AlertEvaluator([rule])
    evaluator.evaluate(
        SliSnapshot(availability=1.0, p95_latency_ms=6000.0, failover_success_rate=None),
        datetime.now(UTC),
    )
    client = TestClient(_app_for(alert_evaluator=evaluator), headers=AUTH_HEADERS)

    response = client.get("/v1/alerts")

    assert response.status_code == 200
    body = response.json()
    assert len(body["alerts"]) == 1
    assert body["alerts"][0]["rule_name"] == "p95-latency-threshold"
    assert body["alerts"][0]["state"] == "firing"


def test_health_ready_triggers_alert_evaluation():
    registry = HostRegistry(new_registry_db_path())
    registry.register(
        "host-a",
        HostCapabilities(backend_type="fake", context_window=8192, base_url="unused"),
        HostCapacity(max_concurrent_requests=1),
        at=datetime.now(UTC),
    )
    registry.acquire_slot("host-a")
    saturation_rule = AlertRule(
        name="host-saturation-threshold",
        kind="threshold",
        metric="host_saturation",
        comparison="gt",
        threshold_value=0.9,
        window=timedelta(minutes=5),
        severity=AlertSeverity.CRITICAL,
        runbook_url="docs/runbooks/host-saturation-threshold.md",
    )
    evaluator = AlertEvaluator([saturation_rule])
    client = TestClient(
        _app_for(alert_evaluator=evaluator, registry=registry), headers=AUTH_HEADERS
    )

    client.get("/health/ready")
    response = client.get("/v1/alerts")

    assert response.status_code == 200
    alerts = response.json()["alerts"]
    assert len(alerts) == 1
    assert alerts[0]["rule_name"] == "host-saturation-threshold:host-a"

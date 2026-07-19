from datetime import UTC, datetime

from fastapi.testclient import TestClient

from llm_home_lab.api.app import create_app
from llm_home_lab.backends.base import BackendHealth, BackendResponse
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


def _app_for(*backends):
    registry = HostRegistry()
    factories = {}
    for backend in backends:
        registry.register(
            backend.backend_id,
            HostCapabilities(
                backend_type=backend.backend_id, context_window=8192, base_url="unused"
            ),
            HostCapacity(max_concurrent_requests=1000),
            at=datetime.now(UTC),
        )
        factories[backend.backend_id] = (lambda b: lambda caps: b)(backend)
    policy = RoutingPolicy(rules=[PolicyRule(name="flat", score_fn=lambda c, ctx: 0.0)])
    return create_app(
        registry=registry,
        router=RoutingEngine(policy),
        health_monitor=HealthMonitor(),
        scheduling_queue=SchedulingQueue(),
        backend_factories=factories,
        metrics_registry=MetricsRegistry(),
        alert_evaluator=AlertEvaluator([]),
        key_store=_permissive_key_store(),
    )


class FakeBackend:
    backend_id = "fake-backend"

    def __init__(self, healthy: bool = True, detail: str = "ok"):
        self._healthy = healthy
        self._detail = detail

    async def complete(self, request):
        return BackendResponse(
            model=request.model,
            content="Hello!",
            finish_reason="stop",
            prompt_tokens=5,
            completion_tokens=2,
        )

    async def stream(self, request):
        return
        yield

    async def check_health(self):
        return BackendHealth(healthy=self._healthy, detail=self._detail)


def test_liveness_always_returns_ok():
    client = TestClient(_app_for(FakeBackend()))

    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_reports_healthy_backend():
    client = TestClient(_app_for(FakeBackend(healthy=True)))

    response = client.get("/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["backends"] == [{"id": "fake-backend", "healthy": True, "detail": "ok"}]


def test_readiness_reports_unhealthy_backend():
    client = TestClient(_app_for(FakeBackend(healthy=False, detail="connection refused")))

    response = client.get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["backends"] == [
        {"id": "fake-backend", "healthy": False, "detail": "connection refused"}
    ]


def test_every_request_produces_one_structured_log_line(caplog):
    client = TestClient(_app_for(FakeBackend()))

    with caplog.at_level("INFO"):
        response = client.get("/health/live")

    assert response.status_code == 200
    lines = [record.message for record in caplog.records if record.name == "llm_home_lab.access"]
    assert len(lines) == 1
    assert "request_id=" in lines[0]
    assert "method=GET" in lines[0]
    assert "status=200" in lines[0]
    assert "latency_ms=" in lines[0]


def test_inbound_request_id_is_reused(caplog):
    client = TestClient(_app_for(FakeBackend()))

    with caplog.at_level("INFO"):
        response = client.get("/health/live", headers={"X-Request-ID": "req-abc-123"})

    assert response.headers["X-Request-ID"] == "req-abc-123"
    access_lines = [
        record.message for record in caplog.records if record.name == "llm_home_lab.access"
    ]
    assert "request_id=req-abc-123" in access_lines[0]


def test_request_id_is_generated_when_absent():
    client = TestClient(_app_for(FakeBackend()))

    response = client.get("/health/live")

    assert response.headers["X-Request-ID"]
    assert len(response.headers["X-Request-ID"]) > 0

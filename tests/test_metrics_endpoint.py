from datetime import UTC, datetime

from fastapi.testclient import TestClient
from registry_test_helpers import inert_external_load_probe, new_registry_db_path

from llm_home_lab.api.app import create_app
from llm_home_lab.backends.base import BackendChunk, BackendConnectionError, BackendResponse
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

AUTH_HEADERS = {"Authorization": "Bearer test-key"}


class FakeBackend:
    backend_id = "host-a"

    async def complete(self, request):
        return BackendResponse(
            model=request.model,
            content="Hello!",
            finish_reason="stop",
            prompt_tokens=10,
            completion_tokens=5,
        )


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


def _app_for(
    registry=None,
    metrics_registry=None,
    alert_evaluator=None,
    backend=None,
    backend_factory=None,
    health_monitor=None,
    extra_hosts=(),
):
    registry = registry or HostRegistry(new_registry_db_path())
    registry.register(
        "host-a",
        HostCapabilities(backend_type="fake", context_window=8192, base_url="unused"),
        HostCapacity(max_concurrent_requests=4),
        at=datetime.now(UTC),
    )
    for host_id in extra_hosts:
        registry.register(
            host_id,
            HostCapabilities(backend_type="fake", context_window=8192, base_url="unused"),
            HostCapacity(max_concurrent_requests=4),
            at=datetime.now(UTC),
        )
    if backend_factory is not None:
        factories = {"fake": lambda caps, f=backend_factory: f()}
    elif backend is not None:
        factories = {"fake": lambda caps, b=backend: b}
    else:
        factories = {}
    policy = RoutingPolicy(rules=[PolicyRule(name="flat", score_fn=lambda c, ctx: 0.0)])
    return create_app(
        registry=registry,
        router=RoutingEngine(policy),
        health_monitor=health_monitor or HealthMonitor(),
        scheduling_queue=SchedulingQueue(),
        backend_factories=factories,
        key_store=_permissive_key_store(),
        metrics_registry=metrics_registry or MetricsRegistry(),
        alert_evaluator=alert_evaluator or AlertEvaluator([]),
        external_load_probe=inert_external_load_probe(),
    )


def test_metrics_endpoint_returns_prometheus_text():
    client = TestClient(_app_for())

    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "llm_home_lab_availability_ratio 1.0" in response.text


def test_metrics_endpoint_is_reachable_without_authentication():
    client = TestClient(_app_for())

    response = client.get("/metrics")

    assert response.status_code != 401
    assert response.status_code != 403


def test_a_successful_chat_completion_records_token_usage():
    metrics_registry = MetricsRegistry()
    client = TestClient(
        _app_for(metrics_registry=metrics_registry, backend=FakeBackend()), headers=AUTH_HEADERS
    )
    payload = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "Hi"}],
        "stream": False,
    }

    client.post("/v1/chat/completions", json=payload)
    response = client.get("/metrics")

    assert 'llm_home_lab_token_usage_total{host_id="host-a"} 15' in response.text


class StreamingFakeBackendWithUsage:
    backend_id = "host-a"

    async def stream(self, request):
        yield BackendChunk(
            content="Hi", finish_reason="stop", usage={"prompt_tokens": 10, "completion_tokens": 5}
        )


def test_a_streaming_chat_completion_records_token_usage_when_the_backend_reports_it():
    metrics_registry = MetricsRegistry()
    client = TestClient(
        _app_for(metrics_registry=metrics_registry, backend=StreamingFakeBackendWithUsage()),
        headers=AUTH_HEADERS,
    )
    payload = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "Hi"}],
        "stream": True,
    }

    client.post("/v1/chat/completions", json=payload)
    response = client.get("/metrics")

    assert 'llm_home_lab_token_usage_total{host_id="host-a"} 15' in response.text


def test_a_failed_request_lowers_the_availability_reported_by_metrics():
    class UnreachableBackend:
        backend_id = "host-a"

        async def complete(self, request):
            raise BackendConnectionError("All connection attempts failed")

    metrics_registry = MetricsRegistry()
    client = TestClient(
        _app_for(metrics_registry=metrics_registry, backend=UnreachableBackend()),
        headers=AUTH_HEADERS,
    )
    payload = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "Hi"}],
        "stream": False,
    }

    client.post("/v1/chat/completions", json=payload)
    response = client.get("/metrics")

    assert "llm_home_lab_availability_ratio 0.0" in response.text


def test_a_request_served_despite_an_unhealthy_candidate_counts_as_a_failover_success():
    health_monitor = HealthMonitor(failure_threshold=1)
    health_monitor.record_probe("host-a", healthy=False, at=datetime.now(UTC))
    metrics_registry = MetricsRegistry()
    client = TestClient(
        _app_for(
            metrics_registry=metrics_registry,
            backend_factory=FakeBackend,
            health_monitor=health_monitor,
            extra_hosts=["host-b"],
        ),
        headers=AUTH_HEADERS,
    )
    payload = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "Hi"}],
        "stream": False,
    }

    client.post("/v1/chat/completions", json=payload)
    response = client.get("/metrics")

    assert "llm_home_lab_failover_success_ratio 1.0" in response.text


class StreamingFakeBackend:
    async def stream(self, request):
        from llm_home_lab.backends.base import BackendChunk

        yield BackendChunk(content="Hi", finish_reason="stop")


def test_a_streaming_request_served_despite_an_unhealthy_candidate_records_failover_success():
    health_monitor = HealthMonitor(failure_threshold=1)
    health_monitor.record_probe("host-a", healthy=False, at=datetime.now(UTC))
    metrics_registry = MetricsRegistry()
    client = TestClient(
        _app_for(
            metrics_registry=metrics_registry,
            backend_factory=StreamingFakeBackend,
            health_monitor=health_monitor,
            extra_hosts=["host-b"],
        ),
        headers=AUTH_HEADERS,
    )
    payload = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "Hi"}],
        "stream": True,
    }

    client.post("/v1/chat/completions", json=payload)
    response = client.get("/metrics")

    assert "llm_home_lab_failover_success_ratio 1.0" in response.text


def test_a_backend_error_despite_an_unhealthy_candidate_records_failover_failure():
    class FailingBackend:
        async def complete(self, request):
            raise BackendConnectionError("All connection attempts failed")

    health_monitor = HealthMonitor(failure_threshold=1)
    health_monitor.record_probe("host-a", healthy=False, at=datetime.now(UTC))
    metrics_registry = MetricsRegistry()
    client = TestClient(
        _app_for(
            metrics_registry=metrics_registry,
            backend_factory=FailingBackend,
            health_monitor=health_monitor,
            extra_hosts=["host-b"],
        ),
        headers=AUTH_HEADERS,
    )
    payload = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "Hi"}],
        "stream": False,
    }

    client.post("/v1/chat/completions", json=payload)
    response = client.get("/metrics")

    assert "llm_home_lab_failover_success_ratio 0.0" in response.text


class StubBackend:
    pass


def test_a_dispatch_timeout_with_an_unhealthy_candidate_records_failover_failure():
    registry = HostRegistry(new_registry_db_path())
    for host_id in ("host-a", "host-b"):
        registry.register(
            host_id,
            HostCapabilities(backend_type="fake", context_window=8192, base_url="unused"),
            HostCapacity(max_concurrent_requests=1),
            at=datetime.now(UTC),
        )
        registry.acquire_slot(host_id)
    health_monitor = HealthMonitor(failure_threshold=1)
    health_monitor.record_probe("host-a", healthy=False, at=datetime.now(UTC))
    metrics_registry = MetricsRegistry()
    policy = RoutingPolicy(rules=[PolicyRule(name="flat", score_fn=lambda c, ctx: 0.0)])
    app = create_app(
        registry=registry,
        router=RoutingEngine(policy),
        health_monitor=health_monitor,
        scheduling_queue=SchedulingQueue(),
        backend_factories={"fake": lambda caps: StubBackend()},
        metrics_registry=metrics_registry,
        alert_evaluator=AlertEvaluator([]),
        key_store=_permissive_key_store(),
        dispatch_wait_timeout=0.05,
        dispatch_poll_interval=0.01,
        external_load_probe=inert_external_load_probe(),
    )
    client = TestClient(app, headers=AUTH_HEADERS)
    payload = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "Hi"}],
        "stream": False,
    }

    response = client.post("/v1/chat/completions", json=payload)
    metrics_response = client.get("/metrics")

    assert response.status_code == 503
    assert "llm_home_lab_failover_success_ratio 0.0" in metrics_response.text

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from registry_test_helpers import inert_external_load_probe, new_registry_db_path

from llm_home_lab.api.app import create_app
from llm_home_lab.backends.base import BackendChunk, BackendError, BackendHealth, BackendResponse
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


class FakeBackend:
    def __init__(
        self,
        backend_id: str,
        healthy: bool = True,
        content: str | None = None,
        finish_reason: str = "stop",
        error: BackendError | None = None,
        stream_chunks: list[BackendChunk] | None = None,
    ) -> None:
        self.backend_id = backend_id
        self.healthy = healthy
        self._content = content
        self._finish_reason = finish_reason
        self._error = error
        self._stream_chunks = stream_chunks or []

    async def complete(self, request):
        if self._error is not None:
            raise self._error
        return BackendResponse(
            model=request.model,
            content=self._content if self._content is not None else f"hi from {self.backend_id}",
            finish_reason=self._finish_reason,
            prompt_tokens=1,
            completion_tokens=1,
        )

    async def stream(self, request):
        for chunk in self._stream_chunks:
            yield chunk

    async def check_health(self):
        return BackendHealth(healthy=self.healthy, detail="ok" if self.healthy else "down")


def _app_for(*backends, failure_threshold=1):
    registry = HostRegistry(new_registry_db_path())
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
    policy = RoutingPolicy(
        rules=[
            PolicyRule(
                name="prefer-first",
                score_fn=lambda c, ctx: 1.0 if c.backend.backend_id == "primary" else 0.0,
            )
        ]
    )
    health_monitor = HealthMonitor(failure_threshold=failure_threshold)
    return create_app(
        registry=registry,
        router=RoutingEngine(policy),
        health_monitor=health_monitor,
        scheduling_queue=SchedulingQueue(),
        backend_factories=factories,
        metrics_registry=MetricsRegistry(),
        alert_evaluator=AlertEvaluator([]),
        key_store=_permissive_key_store(),
        external_load_probe=inert_external_load_probe(),
    )


def test_a_degraded_backend_is_excluded_and_requests_reroute_to_the_healthy_one():
    primary = FakeBackend("primary", healthy=True)
    secondary = FakeBackend("secondary", healthy=True)
    client = TestClient(_app_for(primary, secondary), headers=AUTH_HEADERS)
    payload = {"model": "test-model", "messages": [{"role": "user", "content": "hi"}]}

    assert (
        client.post("/v1/chat/completions", json=payload).json()["choices"][0]["message"]["content"]
        == "hi from primary"
    )

    primary.healthy = False
    client.get("/health/ready")

    response = client.post("/v1/chat/completions", json=payload)

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "hi from secondary"


def test_no_healthy_backends_returns_a_service_unavailable_gateway_error():
    only_backend = FakeBackend("primary", healthy=True)
    client = TestClient(_app_for(only_backend), headers=AUTH_HEADERS)
    payload = {"model": "test-model", "messages": [{"role": "user", "content": "hi"}]}

    only_backend.healthy = False
    client.get("/health/ready")

    response = client.post("/v1/chat/completions", json=payload)

    assert response.status_code == 503
    assert response.json()["error"]["type"] == "backend_error"


def test_repeated_empty_completions_exclude_the_backend_and_reroute():
    primary = FakeBackend("primary", content="")
    secondary = FakeBackend("secondary")
    client = TestClient(_app_for(primary, secondary), headers=AUTH_HEADERS)
    payload = {"model": "test-model", "messages": [{"role": "user", "content": "hi"}]}

    client.post("/v1/chat/completions", json=payload)
    response = client.post("/v1/chat/completions", json=payload)

    assert response.json()["choices"][0]["message"]["content"] == "hi from secondary"


def test_a_non_stop_finish_reason_also_excludes_the_backend():
    primary = FakeBackend("primary", content="truncated answer", finish_reason="length")
    secondary = FakeBackend("secondary")
    client = TestClient(_app_for(primary, secondary), headers=AUTH_HEADERS)
    payload = {"model": "test-model", "messages": [{"role": "user", "content": "hi"}]}

    client.post("/v1/chat/completions", json=payload)
    response = client.post("/v1/chat/completions", json=payload)

    assert response.json()["choices"][0]["message"]["content"] == "hi from secondary"


def test_a_backend_error_from_complete_also_excludes_the_backend():
    primary = FakeBackend("primary", error=BackendError("boom"))
    secondary = FakeBackend("secondary")
    client = TestClient(_app_for(primary, secondary), headers=AUTH_HEADERS)
    payload = {"model": "test-model", "messages": [{"role": "user", "content": "hi"}]}

    client.post("/v1/chat/completions", json=payload)
    response = client.post("/v1/chat/completions", json=payload)

    assert response.json()["choices"][0]["message"]["content"] == "hi from secondary"


def test_streaming_empty_chunks_exclude_the_backend_and_reroute():
    primary = FakeBackend("primary", stream_chunks=[BackendChunk(content="", finish_reason="stop")])
    secondary = FakeBackend("secondary")
    client = TestClient(_app_for(primary, secondary), headers=AUTH_HEADERS)
    payload = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    }

    client.post("/v1/chat/completions", json=payload)
    response = client.post("/v1/chat/completions", json=payload)

    assert response.headers["X-Backend-Id"] == "secondary"

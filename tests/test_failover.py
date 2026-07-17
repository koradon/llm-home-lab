from datetime import UTC, datetime

from fastapi.testclient import TestClient

from llm_home_lab.api.app import create_app
from llm_home_lab.backends.base import BackendHealth, BackendResponse
from llm_home_lab.health.monitor import HealthMonitor
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
    def __init__(self, backend_id: str, healthy: bool = True) -> None:
        self.backend_id = backend_id
        self.healthy = healthy

    async def complete(self, request):
        return BackendResponse(
            model=request.model,
            content=f"hi from {self.backend_id}",
            finish_reason="stop",
            prompt_tokens=1,
            completion_tokens=1,
        )

    async def check_health(self):
        return BackendHealth(healthy=self.healthy, detail="ok" if self.healthy else "down")


def _app_for(*backends, failure_threshold=1):
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
        key_store=_permissive_key_store(),
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

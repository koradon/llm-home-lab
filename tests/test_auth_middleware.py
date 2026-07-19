from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from registry_test_helpers import new_registry_db_path

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

T0 = datetime(2026, 1, 1, tzinfo=UTC)


class FakeBackend:
    backend_id = "fake"

    async def complete(self, request):
        return BackendResponse(
            model=request.model,
            content="hi",
            finish_reason="stop",
            prompt_tokens=1,
            completion_tokens=1,
        )

    async def check_health(self):
        return BackendHealth(healthy=True, detail="ok")


def _key_store(clients=None) -> ApiKeyStore:
    return ApiKeyStore(
        clients
        or [
            ClientConfig(
                client_id="chat-client",
                allowed_path_prefixes=["/v1/chat/completions"],
                keys=[ApiKey(key="sk-chat", expires_at=None)],
            )
        ]
    )


def _app(key_store=None, auth_enabled=True):
    registry = HostRegistry(new_registry_db_path())
    backend = FakeBackend()
    registry.register(
        backend.backend_id,
        HostCapabilities(backend_type="fake", context_window=8192, base_url="unused"),
        HostCapacity(max_concurrent_requests=1000),
        at=T0,
    )
    policy = RoutingPolicy(rules=[PolicyRule(name="flat", score_fn=lambda c, ctx: 0.0)])
    return create_app(
        registry=registry,
        router=RoutingEngine(policy),
        health_monitor=HealthMonitor(),
        scheduling_queue=SchedulingQueue(),
        backend_factories={"fake": lambda caps: backend},
        metrics_registry=MetricsRegistry(),
        alert_evaluator=AlertEvaluator([]),
        key_store=key_store if key_store is not None else (_key_store() if auth_enabled else None),
        auth_enabled=auth_enabled,
    )


PAYLOAD = {"model": "test-model", "messages": [{"role": "user", "content": "hi"}]}


def test_a_request_with_no_authorization_header_is_rejected_with_401():
    client = TestClient(_app())

    response = client.post("/v1/chat/completions", json=PAYLOAD)

    assert response.status_code == 401


def test_a_request_with_an_unrecognized_token_is_rejected_with_401():
    client = TestClient(_app())

    response = client.post(
        "/v1/chat/completions", json=PAYLOAD, headers={"Authorization": "Bearer sk-does-not-exist"}
    )

    assert response.status_code == 401


def test_a_valid_token_within_its_allowed_prefix_reaches_the_route_handler():
    client = TestClient(_app())

    response = client.post(
        "/v1/chat/completions", json=PAYLOAD, headers={"Authorization": "Bearer sk-chat"}
    )

    assert response.status_code == 200


def test_a_valid_token_outside_its_allowed_prefix_is_rejected_with_403():
    client = TestClient(_app())

    response = client.get("/v1/nodes", headers={"Authorization": "Bearer sk-chat"})

    assert response.status_code == 403


def test_health_probes_bypass_authentication():
    client = TestClient(_app())

    live = client.get("/health/live")
    ready = client.get("/health/ready")

    assert live.status_code == 200
    assert ready.status_code in (200, 503)


def test_auth_disabled_allows_a_request_with_no_authorization_header():
    client = TestClient(_app(auth_enabled=False))

    response = client.post("/v1/chat/completions", json=PAYLOAD)

    assert response.status_code == 200


def test_create_app_requires_a_key_store_when_auth_is_enabled():
    registry = HostRegistry(new_registry_db_path())
    policy = RoutingPolicy(rules=[PolicyRule(name="flat", score_fn=lambda c, ctx: 0.0)])

    with pytest.raises(ValueError):
        create_app(
            registry=registry,
            router=RoutingEngine(policy),
            health_monitor=HealthMonitor(),
            scheduling_queue=SchedulingQueue(),
            backend_factories={},
            metrics_registry=MetricsRegistry(),
            alert_evaluator=AlertEvaluator([]),
            key_store=None,
            auth_enabled=True,
        )

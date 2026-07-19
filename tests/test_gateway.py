import json
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from registry_test_helpers import new_registry_db_path

from llm_home_lab.api.app import create_app
from llm_home_lab.backends.base import (
    BackendChunk,
    BackendConnectionError,
    BackendResponse,
    BackendResponseError,
    BackendTimeoutError,
)
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


def _app_for(backend):
    registry = HostRegistry(new_registry_db_path())
    registry.register(
        backend.backend_id,
        HostCapabilities(backend_type="fake", context_window=8192, base_url="unused"),
        HostCapacity(max_concurrent_requests=1000),
        at=datetime.now(UTC),
    )
    policy = RoutingPolicy(rules=[PolicyRule(name="flat", score_fn=lambda c, ctx: 0.0)])
    return create_app(
        registry=registry,
        router=RoutingEngine(policy),
        health_monitor=HealthMonitor(),
        scheduling_queue=SchedulingQueue(),
        backend_factories={"fake": lambda caps, b=backend: b},
        metrics_registry=MetricsRegistry(),
        alert_evaluator=AlertEvaluator([]),
        key_store=_permissive_key_store(),
    )


class FakeBackend:
    backend_id = "fake-backend"

    async def complete(self, request):
        return BackendResponse(
            model=request.model,
            content="Hello!",
            finish_reason="stop",
            prompt_tokens=5,
            completion_tokens=2,
        )

    async def stream(self, request):
        yield BackendChunk(content="Hel", finish_reason=None)
        yield BackendChunk(content="lo!", finish_reason=None)
        yield BackendChunk(content="", finish_reason="stop")


def test_valid_non_streaming_request_returns_openai_shaped_response():
    client = TestClient(_app_for(FakeBackend()), headers=AUTH_HEADERS)
    payload = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "Hi"}],
        "stream": False,
    }

    response = client.post("/v1/chat/completions", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == "test-model"
    assert body["choices"][0]["message"] == {"role": "assistant", "content": "Hello!"}
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"] == {
        "prompt_tokens": 5,
        "completion_tokens": 2,
        "total_tokens": 7,
    }
    assert "id" in body
    assert "created" in body


def test_missing_messages_field_is_rejected_with_error_envelope():
    client = TestClient(_app_for(FakeBackend()), headers=AUTH_HEADERS)
    payload = {"model": "test-model", "stream": False}

    response = client.post("/v1/chat/completions", json=payload)

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["type"] == "invalid_request_error"
    assert "message" in body["error"]
    assert "code" in body["error"]


def test_empty_messages_array_is_rejected():
    client = TestClient(_app_for(FakeBackend()), headers=AUTH_HEADERS)
    payload = {"model": "test-model", "messages": [], "stream": False}

    response = client.post("/v1/chat/completions", json=payload)

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


def test_unrecognized_top_level_field_is_tolerated():
    client = TestClient(_app_for(FakeBackend()), headers=AUTH_HEADERS)
    payload = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "Hi"}],
        "top_p": 0.9,
    }

    response = client.post("/v1/chat/completions", json=payload)

    assert response.status_code == 200


def test_backend_timeout_is_surfaced_as_gateway_error():
    class TimingOutBackend:
        backend_id = "timing-out-backend"

        async def complete(self, request):
            raise BackendTimeoutError("backend did not respond in time")

    client = TestClient(_app_for(TimingOutBackend()), headers=AUTH_HEADERS)
    payload = {"model": "test-model", "messages": [{"role": "user", "content": "Hi"}]}

    response = client.post("/v1/chat/completions", json=payload)

    assert response.status_code == 504
    body = response.json()
    assert body["error"]["type"] == "backend_error"
    assert "message" in body["error"]


def test_backend_connection_failure_is_surfaced_as_service_unavailable():
    class UnreachableBackend:
        backend_id = "unreachable-backend"

        async def complete(self, request):
            raise BackendConnectionError("All connection attempts failed")

    client = TestClient(_app_for(UnreachableBackend()), headers=AUTH_HEADERS)
    payload = {"model": "test-model", "messages": [{"role": "user", "content": "Hi"}]}

    response = client.post("/v1/chat/completions", json=payload)

    assert response.status_code == 503
    body = response.json()
    assert body["error"]["type"] == "backend_error"
    assert "message" in body["error"]


def test_backend_response_error_is_surfaced_as_service_unavailable():
    class FailingBackend:
        backend_id = "failing-backend"

        async def complete(self, request):
            raise BackendResponseError(500, "internal error from backend")

    client = TestClient(_app_for(FailingBackend()), headers=AUTH_HEADERS)
    payload = {"model": "test-model", "messages": [{"role": "user", "content": "Hi"}]}

    response = client.post("/v1/chat/completions", json=payload)

    assert response.status_code == 503
    body = response.json()
    assert body["error"]["type"] == "backend_error"
    assert "message" in body["error"]


def test_malformed_json_body_is_rejected():
    client = TestClient(_app_for(FakeBackend()), headers=AUTH_HEADERS)

    response = client.post(
        "/v1/chat/completions",
        content="{not valid json",
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


def test_streaming_request_returns_sse_chunks_ending_in_done():
    client = TestClient(_app_for(FakeBackend()), headers=AUTH_HEADERS)
    payload = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "Hi"}],
        "stream": True,
    }

    response = client.post("/v1/chat/completions", json=payload)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    lines = [line for line in response.text.split("\n\n") if line]
    assert lines[-1] == "data: [DONE]"
    first_chunk = json.loads(lines[0].removeprefix("data: "))
    assert first_chunk["object"] == "chat.completion.chunk"
    assert first_chunk["choices"][0]["delta"] == {"role": "assistant", "content": "Hel"}
    last_data_chunk = json.loads(lines[-2].removeprefix("data: "))
    assert last_data_chunk["choices"][0]["finish_reason"] == "stop"

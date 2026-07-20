from datetime import UTC, datetime

from fastapi.testclient import TestClient
from registry_test_helpers import inert_external_load_probe, new_registry_db_path

from llm_home_lab.api.app import create_app
from llm_home_lab.backends.base import BackendResponse
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
AUTH_HEADERS = {"Authorization": "Bearer test-key"}


class FakeBackend:
    def __init__(self, backend_id: str = "fake") -> None:
        self.backend_id = backend_id

    async def complete(self, request):
        return BackendResponse(
            model=request.model,
            content="hi",
            finish_reason="stop",
            prompt_tokens=1,
            completion_tokens=1,
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


def _app(
    backend,
    allowed_models,
    memory_budget_gb=None,
    model_sizes_gb=None,
):
    registry = HostRegistry(new_registry_db_path())
    registry.register(
        backend.backend_id,
        HostCapabilities(
            backend_type="fake",
            context_window=8192,
            base_url="unused",
            allowed_models=allowed_models,
            memory_budget_gb=memory_budget_gb,
            model_sizes_gb=model_sizes_gb,
        ),
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
        key_store=_permissive_key_store(),
        external_load_probe=inert_external_load_probe(),
    )


def _payload(model: str) -> dict:
    return {"model": model, "messages": [{"role": "user", "content": "hi"}]}


def test_a_model_not_in_the_hosts_static_allow_list_is_rejected_with_400():
    client = TestClient(
        _app(FakeBackend(), allowed_models=["qwen2.5-coder-14b-instruct-mlx"]), headers=AUTH_HEADERS
    )

    response = client.post("/v1/chat/completions", json=_payload("some-other-model"))

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


def test_a_model_in_the_hosts_static_allow_list_is_admitted():
    client = TestClient(
        _app(FakeBackend(), allowed_models=["qwen2.5-coder-14b-instruct-mlx"]), headers=AUTH_HEADERS
    )

    response = client.post("/v1/chat/completions", json=_payload("qwen2.5-coder-14b-instruct-mlx"))

    assert response.status_code == 200


class BackendWithModelIntrospection(FakeBackend):
    def __init__(self, loaded_models):
        super().__init__()
        self._loaded_models = loaded_models

    async def list_models(self):
        return self._loaded_models


def test_no_static_list_but_backend_reports_the_model_is_not_loaded_is_rejected_with_400():
    backend = BackendWithModelIntrospection(loaded_models=["only-this-one"])
    client = TestClient(_app(backend, allowed_models=None), headers=AUTH_HEADERS)

    response = client.post("/v1/chat/completions", json=_payload("some-other-model"))

    assert response.status_code == 400


def test_no_static_list_and_backend_reports_the_model_is_loaded_is_admitted():
    backend = BackendWithModelIntrospection(loaded_models=["some-other-model"])
    client = TestClient(_app(backend, allowed_models=None), headers=AUTH_HEADERS)

    response = client.post("/v1/chat/completions", json=_payload("some-other-model"))

    assert response.status_code == 200


def test_backend_without_list_models_support_is_permissive():
    client = TestClient(_app(FakeBackend(), allowed_models=None), headers=AUTH_HEADERS)

    response = client.post("/v1/chat/completions", json=_payload("anything-at-all"))

    assert response.status_code == 200


class BackendWhoseModelListFails(FakeBackend):
    async def list_models(self):
        return None


def test_backend_whose_list_models_call_fails_is_permissive():
    client = TestClient(
        _app(BackendWhoseModelListFails(), allowed_models=None), headers=AUTH_HEADERS
    )

    response = client.post("/v1/chat/completions", json=_payload("anything-at-all"))

    assert response.status_code == 200


def test_on_demand_load_within_budget_is_admitted():
    backend = BackendWithModelIntrospection(loaded_models=["small-model-a"])
    client = TestClient(
        _app(
            backend,
            allowed_models=None,
            memory_budget_gb=16.0,
            model_sizes_gb={"small-model-a": 4.0, "small-model-b": 4.0},
        ),
        headers=AUTH_HEADERS,
    )

    response = client.post("/v1/chat/completions", json=_payload("small-model-b"))

    assert response.status_code == 200


def test_on_demand_load_exceeding_budget_is_rejected_with_503():
    backend = BackendWithModelIntrospection(loaded_models=["big-model-a"])
    client = TestClient(
        _app(
            backend,
            allowed_models=None,
            memory_budget_gb=16.0,
            model_sizes_gb={"big-model-a": 14.0, "big-model-b": 14.0},
        ),
        headers=AUTH_HEADERS,
    )

    response = client.post("/v1/chat/completions", json=_payload("big-model-b"))

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "model_capacity_exceeded"


def test_on_demand_load_of_a_model_with_unknown_size_is_rejected_with_503():
    backend = BackendWithModelIntrospection(loaded_models=["small-model-a"])
    client = TestClient(
        _app(
            backend,
            allowed_models=None,
            memory_budget_gb=16.0,
            model_sizes_gb={"small-model-a": 4.0},
        ),
        headers=AUTH_HEADERS,
    )

    response = client.post("/v1/chat/completions", json=_payload("model-of-unknown-size"))

    assert response.status_code == 503


def test_on_demand_load_when_a_currently_loaded_models_size_is_unknown_is_rejected_with_503():
    backend = BackendWithModelIntrospection(loaded_models=["model-of-unknown-size"])
    client = TestClient(
        _app(
            backend,
            allowed_models=None,
            memory_budget_gb=16.0,
            model_sizes_gb={"small-model-b": 4.0},
        ),
        headers=AUTH_HEADERS,
    )

    response = client.post("/v1/chat/completions", json=_payload("small-model-b"))

    assert response.status_code == 503


def test_an_already_loaded_model_is_admitted_regardless_of_budget():
    backend = BackendWithModelIntrospection(loaded_models=["big-model-a"])
    client = TestClient(
        _app(
            backend,
            allowed_models=None,
            memory_budget_gb=1.0,
            model_sizes_gb={"big-model-a": 100.0},
        ),
        headers=AUTH_HEADERS,
    )

    response = client.post("/v1/chat/completions", json=_payload("big-model-a"))

    assert response.status_code == 200

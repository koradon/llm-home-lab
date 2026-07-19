from datetime import timedelta

from fastapi.testclient import TestClient

from llm_home_lab.api.app import create_app
from llm_home_lab.health.monitor import HealthMonitor
from llm_home_lab.observability.alerts import AlertEvaluator
from llm_home_lab.observability.metrics import MetricsRegistry
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
    def __init__(self, backend_id: str = "fake", detail: str = "ok") -> None:
        self.backend_id = backend_id
        self.detail = detail

    async def check_health(self):
        from llm_home_lab.backends.base import BackendHealth

        return BackendHealth(healthy=True, detail=self.detail)


def _app(heartbeat_ttl=timedelta(seconds=60), backend_factory=None):
    policy = RoutingPolicy(rules=[PolicyRule(name="flat", score_fn=lambda c, ctx: 0.0)])
    return create_app(
        registry=HostRegistry(),
        router=RoutingEngine(policy),
        health_monitor=HealthMonitor(),
        scheduling_queue=SchedulingQueue(),
        backend_factories={"fake": backend_factory or (lambda caps: FakeBackend())},
        metrics_registry=MetricsRegistry(),
        alert_evaluator=AlertEvaluator([]),
        key_store=_permissive_key_store(),
        heartbeat_ttl=heartbeat_ttl,
    )


def _register_payload(host_id: str = "host-a") -> dict:
    return {
        "host_id": host_id,
        "backend_type": "fake",
        "context_window": 8192,
        "base_url": "http://localhost:1234",
        "max_concurrent_requests": 2,
    }


def test_registering_a_host_makes_it_appear_in_the_node_list():
    client = TestClient(_app(), headers=AUTH_HEADERS)

    response = client.post("/v1/nodes/register", json=_register_payload())

    assert response.status_code == 200
    nodes = client.get("/v1/nodes").json()["nodes"]
    assert [n["host_id"] for n in nodes] == ["host-a"]


def test_registering_a_host_with_allowed_models_threads_them_into_node_metadata():
    client = TestClient(_app(), headers=AUTH_HEADERS)
    payload = _register_payload()
    payload["allowed_models"] = ["qwen2.5-coder-14b-instruct-mlx"]

    client.post("/v1/nodes/register", json=payload)

    nodes = client.get("/v1/nodes").json()["nodes"]
    assert nodes[0]["allowed_models"] == ["qwen2.5-coder-14b-instruct-mlx"]


def test_registering_a_host_with_a_memory_budget_threads_it_into_node_metadata():
    client = TestClient(_app(), headers=AUTH_HEADERS)
    payload = _register_payload()
    payload["memory_budget_gb"] = 24.0
    payload["model_sizes_gb"] = {"qwen2.5-coder-14b-instruct-mlx": 8.5}

    client.post("/v1/nodes/register", json=payload)

    nodes = client.get("/v1/nodes").json()["nodes"]
    assert nodes[0]["memory_budget_gb"] == 24.0
    assert nodes[0]["model_sizes_gb"] == {"qwen2.5-coder-14b-instruct-mlx": 8.5}


def test_registering_a_host_without_allowed_models_defaults_to_none():
    client = TestClient(_app(), headers=AUTH_HEADERS)

    client.post("/v1/nodes/register", json=_register_payload())

    nodes = client.get("/v1/nodes").json()["nodes"]
    assert nodes[0]["allowed_models"] is None


def test_heartbeat_on_an_unregistered_host_returns_404():
    client = TestClient(_app(), headers=AUTH_HEADERS)

    response = client.post("/v1/nodes/host-a/heartbeat")

    assert response.status_code == 404
    assert response.json()["error"]["type"] == "invalid_request_error"


def test_heartbeat_on_a_registered_host_succeeds():
    client = TestClient(_app(), headers=AUTH_HEADERS)
    client.post("/v1/nodes/register", json=_register_payload())

    response = client.post("/v1/nodes/host-a/heartbeat")

    assert response.status_code == 200


def test_heartbeat_on_a_host_id_containing_slashes_succeeds():
    client = TestClient(_app(), headers=AUTH_HEADERS)
    host_id = "http://localhost:1234"
    client.post("/v1/nodes/register", json=_register_payload(host_id))

    response = client.post(f"/v1/nodes/{host_id}/heartbeat")

    assert response.status_code == 200


def test_deregistering_a_host_removes_it_from_the_node_list():
    client = TestClient(_app(), headers=AUTH_HEADERS)
    client.post("/v1/nodes/register", json=_register_payload())

    response = client.delete("/v1/nodes/host-a")

    assert response.status_code == 200
    assert client.get("/v1/nodes").json()["nodes"] == []


def test_deregistering_a_host_prunes_its_cached_backend_without_error():
    client = TestClient(_app(), headers=AUTH_HEADERS)
    client.post("/v1/nodes/register", json=_register_payload())
    client.get("/health/ready")

    response = client.delete("/v1/nodes/host-a")

    assert response.status_code == 200
    assert client.get("/health/ready").json() == {"status": "ok", "backends": []}


def test_deregistering_a_host_id_containing_slashes_removes_it_from_the_node_list():
    client = TestClient(_app(), headers=AUTH_HEADERS)
    host_id = "http://localhost:1234"
    client.post("/v1/nodes/register", json=_register_payload(host_id))

    response = client.delete(f"/v1/nodes/{host_id}")

    assert response.status_code == 200
    assert client.get("/v1/nodes").json()["nodes"] == []


def test_reregistering_a_host_with_a_different_base_url_reconstructs_the_cached_backend():
    constructed_base_urls: list[str] = []

    def factory(capabilities):
        constructed_base_urls.append(capabilities.base_url)
        return FakeBackend(detail=capabilities.base_url)

    client = TestClient(_app(backend_factory=factory), headers=AUTH_HEADERS)
    payload = _register_payload()
    payload["base_url"] = "http://old-host:1234"
    client.post("/v1/nodes/register", json=payload)
    client.get("/health/ready")

    payload["base_url"] = "http://new-host:1234"
    client.post("/v1/nodes/register", json=payload)
    response = client.get("/health/ready")

    assert response.json()["backends"][0]["detail"] == "http://new-host:1234"
    assert constructed_base_urls == ["http://old-host:1234", "http://new-host:1234"]


def test_reregistering_a_host_with_unchanged_capabilities_does_not_reconstruct_the_backend():
    constructed_base_urls: list[str] = []

    def factory(capabilities):
        constructed_base_urls.append(capabilities.base_url)
        return FakeBackend(detail=capabilities.base_url)

    client = TestClient(_app(backend_factory=factory), headers=AUTH_HEADERS)
    client.post("/v1/nodes/register", json=_register_payload())
    client.get("/health/ready")

    client.post("/v1/nodes/register", json=_register_payload())
    client.get("/health/ready")

    assert constructed_base_urls == ["http://localhost:1234"]


def test_health_ready_expires_hosts_stale_past_the_heartbeat_ttl():
    client = TestClient(_app(heartbeat_ttl=timedelta(seconds=0)), headers=AUTH_HEADERS)
    client.post("/v1/nodes/register", json=_register_payload())
    assert client.get("/v1/nodes").json()["nodes"] != []

    client.get("/health/ready")

    assert client.get("/v1/nodes").json()["nodes"] == []

from datetime import timedelta

from fastapi.testclient import TestClient

from llm_home_lab.api.app import create_app
from llm_home_lab.health.monitor import HealthMonitor
from llm_home_lab.registry.registry import HostRegistry
from llm_home_lab.routing.engine import RoutingEngine
from llm_home_lab.routing.models import PolicyRule, RoutingPolicy
from llm_home_lab.scheduling.queue import SchedulingQueue


class FakeBackend:
    def __init__(self, backend_id: str = "fake") -> None:
        self.backend_id = backend_id

    async def check_health(self):
        from llm_home_lab.backends.base import BackendHealth

        return BackendHealth(healthy=True, detail="ok")


def _app(heartbeat_ttl=timedelta(seconds=60)):
    policy = RoutingPolicy(rules=[PolicyRule(name="flat", score_fn=lambda c, ctx: 0.0)])
    return create_app(
        registry=HostRegistry(),
        router=RoutingEngine(policy),
        health_monitor=HealthMonitor(),
        scheduling_queue=SchedulingQueue(),
        backend_factories={"fake": lambda caps: FakeBackend()},
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
    client = TestClient(_app())

    response = client.post("/v1/nodes/register", json=_register_payload())

    assert response.status_code == 200
    nodes = client.get("/v1/nodes").json()["nodes"]
    assert [n["host_id"] for n in nodes] == ["host-a"]


def test_heartbeat_on_an_unregistered_host_returns_404():
    client = TestClient(_app())

    response = client.post("/v1/nodes/host-a/heartbeat")

    assert response.status_code == 404
    assert response.json()["error"]["type"] == "invalid_request_error"


def test_heartbeat_on_a_registered_host_succeeds():
    client = TestClient(_app())
    client.post("/v1/nodes/register", json=_register_payload())

    response = client.post("/v1/nodes/host-a/heartbeat")

    assert response.status_code == 200


def test_deregistering_a_host_removes_it_from_the_node_list():
    client = TestClient(_app())
    client.post("/v1/nodes/register", json=_register_payload())

    response = client.delete("/v1/nodes/host-a")

    assert response.status_code == 200
    assert client.get("/v1/nodes").json()["nodes"] == []


def test_deregistering_a_host_prunes_its_cached_backend_without_error():
    client = TestClient(_app())
    client.post("/v1/nodes/register", json=_register_payload())
    client.get("/health/ready")

    response = client.delete("/v1/nodes/host-a")

    assert response.status_code == 200
    assert client.get("/health/ready").json() == {"status": "ok", "backends": []}


def test_health_ready_expires_hosts_stale_past_the_heartbeat_ttl():
    client = TestClient(_app(heartbeat_ttl=timedelta(seconds=0)))
    client.post("/v1/nodes/register", json=_register_payload())
    assert client.get("/v1/nodes").json()["nodes"] != []

    client.get("/health/ready")

    assert client.get("/v1/nodes").json()["nodes"] == []

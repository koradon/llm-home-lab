import asyncio
import json
import sqlite3
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from registry_test_helpers import inert_external_load_probe, new_registry_db_path

from llm_home_lab.api.app import create_app
from llm_home_lab.backends.base import BackendHealth, BackendResponse
from llm_home_lab.health.monitor import HealthMonitor
from llm_home_lab.leases.models import LeaseSettings
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


class _FakeBackend:
    def __init__(self, *, healthy: bool = True, models: list[str] | None = None) -> None:
        self.backend_id = "fake"
        self._healthy = healthy
        self._models = models

    async def check_health(self) -> BackendHealth:
        return BackendHealth(healthy=self._healthy, detail="ok" if self._healthy else "down")

    async def list_models(self) -> list[str] | None:
        return self._models

    async def complete(self, request) -> BackendResponse:
        return BackendResponse(
            model=request.model,
            content="ok",
            finish_reason="stop",
            prompt_tokens=1,
            completion_tokens=1,
        )


class _BlockingBackend(_FakeBackend):
    def __init__(self, expected_concurrent: int = 1) -> None:
        super().__init__()
        self.in_flight = 0
        self.peak_in_flight = 0
        self._expected_concurrent = expected_concurrent
        self.reached_expected = asyncio.Event()
        self.release_event = asyncio.Event()

    async def complete(self, request) -> BackendResponse:
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        if self.in_flight >= self._expected_concurrent:
            self.reached_expected.set()
        try:
            await self.release_event.wait()
        finally:
            self.in_flight -= 1
        return await super().complete(request)


def _app(
    *,
    db_path: str | None = None,
    backend_factory=None,
    health_monitor: HealthMonitor | None = None,
    lease_settings: LeaseSettings | None = None,
    dispatch_wait_timeout: float = 1.0,
):
    policy = RoutingPolicy(rules=[PolicyRule(name="flat", score_fn=lambda c, ctx: 0.0)])
    return create_app(
        registry=HostRegistry(db_path or new_registry_db_path()),
        router=RoutingEngine(policy),
        health_monitor=health_monitor or HealthMonitor(),
        scheduling_queue=SchedulingQueue(),
        backend_factories={"fake": backend_factory or (lambda caps: _FakeBackend())},
        metrics_registry=MetricsRegistry(),
        alert_evaluator=AlertEvaluator([]),
        key_store=_permissive_key_store(),
        external_load_probe=inert_external_load_probe(),
        lease_settings=lease_settings or LeaseSettings(min_active_nodes=0),
        dispatch_wait_timeout=dispatch_wait_timeout,
        dispatch_poll_interval=0.01,
    )


def _node_payload(host_id: str = "mini-3", **overrides) -> dict:
    base = {
        "host_id": host_id,
        "backend_type": "fake",
        "context_window": 8192,
        "base_url": f"http://{host_id}.local:8081",
        "max_concurrent_requests": 4,
    }
    return base | overrides


def test_registering_a_node_with_model_limits_exposes_effective_values_in_nodes_endpoint():
    client = TestClient(_app(), headers=AUTH_HEADERS)
    payload = _node_payload(
        allowed_models=["gemma-4-26B-A4B"],
        model_limits={
            "gemma-4-26B-A4B": {"max_concurrent_requests": 8, "context_window": 16384},
            "Hy-MT2-30B-A3B": {"max_concurrent_requests": 6},
        },
    )

    response = client.post("/v1/nodes/register", json=payload)

    assert response.status_code == 200
    node = client.get("/v1/nodes").json()["nodes"][0]
    assert node["max_concurrent_requests"] == 4
    assert node["effective_max_concurrent_requests"] == 8
    assert node["context_window"] == 8192
    assert node["effective_context_window"] == 16384
    assert node["model_limits"] == {
        "gemma-4-26B-A4B": {"max_concurrent_requests": 8, "context_window": 16384},
        "Hy-MT2-30B-A3B": {"max_concurrent_requests": 6},
    }


def test_flip_patch_of_allowed_models_updates_served_model_and_effective_limits_together():
    client = TestClient(_app(), headers=AUTH_HEADERS)
    client.post(
        "/v1/nodes/register",
        json=_node_payload(
            allowed_models=["gemma-4-26B-A4B"],
            model_limits={
                "gemma-4-26B-A4B": {"max_concurrent_requests": 8, "context_window": 16384},
                "Hy-MT2-30B-A3B": {"max_concurrent_requests": 6},
            },
        ),
    )
    client.get("/health/ready")

    patch_response = client.patch("/v1/nodes/mini-3", json={"allowed_models": ["Hy-MT2-30B-A3B"]})

    assert patch_response.status_code == 200
    node = client.get("/v1/nodes").json()["nodes"][0]
    assert node["allowed_models"] == ["Hy-MT2-30B-A3B"]
    assert node["max_concurrent_requests"] == 4
    assert node["effective_max_concurrent_requests"] == 6
    assert node["effective_context_window"] == 8192
    assert client.get("/v1/capacity", params={"model": "Hy-MT2-30B-A3B"}).json() == {
        "total_max_concurrent_requests": 6
    }
    assert client.get("/v1/capacity", params={"model": "gemma-4-26B-A4B"}).json() == {
        "total_max_concurrent_requests": 0
    }
    gemma_res = client.post(
        "/v1/chat/completions",
        json={"model": "gemma-4-26B-A4B", "messages": [{"role": "user", "content": "hi"}]},
    )
    hymt_res = client.post(
        "/v1/chat/completions",
        json={"model": "Hy-MT2-30B-A3B", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert gemma_res.status_code == 400
    assert hymt_res.status_code == 200


def test_fallback_to_node_level_limits_when_model_has_no_override():
    client = TestClient(_app(), headers=AUTH_HEADERS)
    client.post(
        "/v1/nodes/register",
        json=_node_payload(
            max_concurrent_requests=4,
            context_window=8192,
            allowed_models=["EuroLLM-22B"],
            model_limits={"gemma-4-26B-A4B": {"max_concurrent_requests": 8}},
        ),
    )
    client.get("/health/ready")

    node = client.get("/v1/nodes").json()["nodes"][0]
    capacity = client.get("/v1/capacity", params={"model": "EuroLLM-22B"}).json()

    assert node["effective_max_concurrent_requests"] == 4
    assert node["effective_context_window"] == 8192
    assert capacity == {"total_max_concurrent_requests": 4}


def test_old_config_without_model_limits_behaves_exactly_as_before():
    db_path = new_registry_db_path()
    client = TestClient(_app(db_path=db_path), headers=AUTH_HEADERS)
    client.post("/v1/nodes/register", json=_node_payload(allowed_models=["gemma-4-e4b"]))
    with sqlite3.connect(db_path) as conn:
        (raw,) = conn.execute("SELECT capabilities FROM hosts").fetchone()
        legacy = {k: v for k, v in json.loads(raw).items() if k != "model_limits"}
        conn.execute("UPDATE hosts SET capabilities = ?", (json.dumps(legacy),))

    restarted = TestClient(_app(db_path=db_path), headers=AUTH_HEADERS)
    restarted.get("/health/ready")

    node = restarted.get("/v1/nodes").json()["nodes"][0]
    assert node["model_limits"] is None
    assert node["max_concurrent_requests"] == 4
    assert node["effective_max_concurrent_requests"] == 4
    assert node["context_window"] == 8192
    assert node["effective_context_window"] == 8192
    assert restarted.get("/v1/capacity").json() == {"total_max_concurrent_requests": 4}
    assert restarted.get("/v1/capacity", params={"model": "gemma-4-e4b"}).json() == {
        "total_max_concurrent_requests": 4
    }


@pytest.mark.parametrize(
    "bad_limits",
    [
        {"gemma": {"max_concurrent_requests": 0}},
        {"gemma": {"max_concurrent_requests": -2}},
        {"gemma": {"context_window": 0}},
    ],
)
def test_invalid_model_limits_are_rejected_with_400(bad_limits):
    client = TestClient(_app(), headers=AUTH_HEADERS)

    response = client.post("/v1/nodes/register", json=_node_payload(model_limits=bad_limits))

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


async def test_dispatch_gate_enforces_per_model_limit_lower_than_node_level():
    backend = _BlockingBackend(expected_concurrent=1)
    app = _app(backend_factory=lambda caps: backend)
    transport = httpx.ASGITransport(app=app)
    payload = {"model": "heavy-model", "messages": [{"role": "user", "content": "hi"}]}

    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=AUTH_HEADERS
    ) as client:
        await client.post(
            "/v1/nodes/register",
            json=_node_payload(
                max_concurrent_requests=4,
                allowed_models=["heavy-model"],
                model_limits={"heavy-model": {"max_concurrent_requests": 1}},
            ),
        )
        first = asyncio.create_task(client.post("/v1/chat/completions", json=payload))
        await backend.reached_expected.wait()

        second = asyncio.create_task(client.post("/v1/chat/completions", json=payload))
        await asyncio.sleep(0.05)

        assert not second.done()
        assert backend.peak_in_flight == 1

        backend.release_event.set()
        first_res, second_res = await first, await second

    assert first_res.status_code == 200
    assert second_res.status_code == 200


async def test_dispatch_gate_enforces_per_model_limit_higher_than_node_level():
    backend = _BlockingBackend(expected_concurrent=2)
    app = _app(backend_factory=lambda caps: backend)
    transport = httpx.ASGITransport(app=app)
    payload = {"model": "fast-model", "messages": [{"role": "user", "content": "hi"}]}

    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=AUTH_HEADERS
    ) as client:
        await client.post(
            "/v1/nodes/register",
            json=_node_payload(
                max_concurrent_requests=1,
                allowed_models=["fast-model"],
                model_limits={"fast-model": {"max_concurrent_requests": 2}},
            ),
        )
        first = asyncio.create_task(client.post("/v1/chat/completions", json=payload))
        second = asyncio.create_task(client.post("/v1/chat/completions", json=payload))
        await backend.reached_expected.wait()

        third = asyncio.create_task(client.post("/v1/chat/completions", json=payload))
        await asyncio.sleep(0.05)

        assert not third.done()
        assert backend.peak_in_flight == 2

        backend.release_event.set()
        responses = await asyncio.gather(first, second, third)

    assert [r.status_code for r in responses] == [200, 200, 200]


def test_routing_uses_per_model_context_window_override():
    client = TestClient(_app(), headers=AUTH_HEADERS)
    client.post(
        "/v1/nodes/register",
        json=_node_payload(
            context_window=4,
            allowed_models=["big-ctx", "default-ctx"],
            model_limits={"big-ctx": {"context_window": 16384}},
        ),
    )
    long_prompt = "word " * 40

    big_res = client.post(
        "/v1/chat/completions",
        json={"model": "big-ctx", "messages": [{"role": "user", "content": long_prompt}]},
    )
    default_res = client.post(
        "/v1/chat/completions",
        json={"model": "default-ctx", "messages": [{"role": "user", "content": long_prompt}]},
    )

    assert big_res.status_code == 200
    assert default_res.status_code == 503


def test_capacity_model_filter_excludes_offline_leased_and_non_matching_hosts():
    health_monitor = HealthMonitor(failure_threshold=1)
    backends = {
        "http://mini-1.local:8081": _FakeBackend(healthy=True),
        "http://mini-2.local:8081": _FakeBackend(healthy=True),
        "http://mini-3.local:8081": _FakeBackend(healthy=False),
        "http://mini-4.local:8081": _FakeBackend(healthy=True),
    }
    client = TestClient(
        _app(
            backend_factory=lambda caps: backends[caps.base_url],
            health_monitor=health_monitor,
        ),
        headers=AUTH_HEADERS,
    )
    for host_id, models, limits in (
        ("mini-1", ["gemma"], {"gemma": {"max_concurrent_requests": 8}}),
        ("mini-2", ["gemma"], {"gemma": {"max_concurrent_requests": 8}}),
        ("mini-3", ["gemma"], {"gemma": {"max_concurrent_requests": 8}}),
        ("mini-4", ["hy-mt2"], {"hy-mt2": {"max_concurrent_requests": 6}}),
    ):
        client.post(
            "/v1/nodes/register",
            json=_node_payload(host_id, allowed_models=models, model_limits=limits),
        )
    client.get("/health/ready")
    lease_res = client.post("/v1/node-leases", json={"holder": "canary-check", "host_id": "mini-2"})
    assert lease_res.status_code == 201

    gemma_capacity = client.get("/v1/capacity", params={"model": "gemma"}).json()
    hymt_capacity = client.get("/v1/capacity", params={"model": "hy-mt2"}).json()
    unknown_capacity = client.get("/v1/capacity", params={"model": "unknown-model"}).json()
    total_capacity = client.get("/v1/capacity").json()

    assert gemma_capacity == {"total_max_concurrent_requests": 8}
    assert hymt_capacity == {"total_max_concurrent_requests": 6}
    assert unknown_capacity == {"total_max_concurrent_requests": 0}
    assert total_capacity == {"total_max_concurrent_requests": 22}


def test_capacity_model_filter_works_for_hosts_without_allowed_models():
    health_monitor = HealthMonitor()
    backend = _FakeBackend(healthy=True, models=["loaded-model"])
    client = TestClient(
        _app(backend_factory=lambda caps: backend, health_monitor=health_monitor),
        headers=AUTH_HEADERS,
    )
    client.post(
        "/v1/nodes/register",
        json=_node_payload(
            "dynamic-host",
            max_concurrent_requests=3,
            model_limits={"loaded-model": {"max_concurrent_requests": 5}},
        ),
    )
    health_monitor.record_probe("dynamic-host", healthy=True, at=datetime.now(UTC))

    loaded_capacity = client.get("/v1/capacity", params={"model": "loaded-model"}).json()
    other_capacity = client.get("/v1/capacity", params={"model": "not-loaded"}).json()

    assert loaded_capacity == {"total_max_concurrent_requests": 5}
    assert other_capacity == {"total_max_concurrent_requests": 0}

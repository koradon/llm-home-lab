from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from registry_test_helpers import inert_external_load_probe, new_registry_db_path

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


def _app(
    db_path=None,
    backend_factory=None,
    external_load_probe=None,
    health_monitor=None,
    backend_factories=None,
    completion_log=None,
):
    policy = RoutingPolicy(rules=[PolicyRule(name="flat", score_fn=lambda c, ctx: 0.0)])
    return create_app(
        registry=HostRegistry(db_path or new_registry_db_path()),
        router=RoutingEngine(policy),
        health_monitor=health_monitor or HealthMonitor(),
        scheduling_queue=SchedulingQueue(),
        backend_factories=(
            backend_factories or {"fake": backend_factory or (lambda caps: FakeBackend())}
        ),
        metrics_registry=MetricsRegistry(),
        alert_evaluator=AlertEvaluator([]),
        key_store=_permissive_key_store(),
        external_load_probe=external_load_probe or inert_external_load_probe(),
        completion_log=completion_log,
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


def test_registering_a_host_with_model_aliases_threads_them_into_node_metadata():
    client = TestClient(_app(), headers=AUTH_HEADERS)
    payload = _register_payload()
    payload["allowed_models"] = ["google/gemma-4-e4b"]
    payload["model_aliases"] = {"google/gemma-4-e4b": ["gemma-a", "gemma-b"]}

    client.post("/v1/nodes/register", json=payload)

    nodes = client.get("/v1/nodes").json()["nodes"]
    assert nodes[0]["model_aliases"] == {"google/gemma-4-e4b": ["gemma-a", "gemma-b"]}


def test_registering_a_host_without_model_aliases_defaults_to_none():
    client = TestClient(_app(), headers=AUTH_HEADERS)

    client.post("/v1/nodes/register", json=_register_payload())

    nodes = client.get("/v1/nodes").json()["nodes"]
    assert nodes[0]["model_aliases"] is None


def test_a_freshly_registered_host_reports_zero_recent_failures():
    client = TestClient(_app(), headers=AUTH_HEADERS)

    client.post("/v1/nodes/register", json=_register_payload())

    nodes = client.get("/v1/nodes").json()["nodes"]
    assert nodes[0]["health"]["recent_failures"] == 0


def test_a_host_with_recorded_failures_reports_its_recent_failure_count():
    health_monitor = HealthMonitor(failure_threshold=100)
    client = TestClient(_app(health_monitor=health_monitor), headers=AUTH_HEADERS)
    client.post("/v1/nodes/register", json=_register_payload())

    health_monitor.record_probe("host-a", healthy=False, at=datetime.now(UTC))
    health_monitor.record_probe("host-a", healthy=False, at=datetime.now(UTC))

    nodes = client.get("/v1/nodes").json()["nodes"]
    assert nodes[0]["health"]["recent_failures"] == 2


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


def test_patching_a_host_updates_only_the_given_field():
    client = TestClient(_app(), headers=AUTH_HEADERS)
    client.post("/v1/nodes/register", json=_register_payload())

    response = client.patch("/v1/nodes/host-a", json={"max_concurrent_requests": 8})

    assert response.status_code == 200
    node = client.get("/v1/nodes").json()["nodes"][0]
    assert node["max_concurrent_requests"] == 8


def test_patching_a_host_preserves_fields_not_included_in_the_payload():
    client = TestClient(_app(), headers=AUTH_HEADERS)
    payload = _register_payload()
    payload["allowed_models"] = ["qwen2.5-coder-14b-instruct-mlx"]
    client.post("/v1/nodes/register", json=payload)

    client.patch("/v1/nodes/host-a", json={"max_concurrent_requests": 8})

    node = client.get("/v1/nodes").json()["nodes"][0]
    assert node["context_window"] == 8192
    assert node["base_url"] == "http://localhost:1234"
    assert node["allowed_models"] == ["qwen2.5-coder-14b-instruct-mlx"]


def test_patching_a_host_with_an_explicit_null_clears_the_field():
    client = TestClient(_app(), headers=AUTH_HEADERS)
    payload = _register_payload()
    payload["allowed_models"] = ["qwen2.5-coder-14b-instruct-mlx"]
    client.post("/v1/nodes/register", json=payload)

    client.patch("/v1/nodes/host-a", json={"allowed_models": None})

    node = client.get("/v1/nodes").json()["nodes"][0]
    assert node["allowed_models"] is None


def test_patching_an_unregistered_host_returns_404():
    client = TestClient(_app(), headers=AUTH_HEADERS)

    response = client.patch("/v1/nodes/host-a", json={"max_concurrent_requests": 8})

    assert response.status_code == 404
    assert response.json()["error"]["type"] == "invalid_request_error"


def test_patching_a_host_id_containing_slashes_succeeds():
    client = TestClient(_app(), headers=AUTH_HEADERS)
    host_id = "http://localhost:1234"
    client.post("/v1/nodes/register", json=_register_payload(host_id))

    response = client.patch(f"/v1/nodes/{host_id}", json={"max_concurrent_requests": 8})

    assert response.status_code == 200


def test_patching_a_host_does_not_reset_its_in_flight_count():
    client = TestClient(_app(), headers=AUTH_HEADERS)
    client.post("/v1/nodes/register", json=_register_payload())
    client.get("/health/ready")

    client.patch("/v1/nodes/host-a", json={"max_concurrent_requests": 8})

    node = client.get("/v1/nodes").json()["nodes"][0]
    assert node["in_flight"] == 0


def test_patching_a_host_with_a_different_base_url_reconstructs_the_cached_backend():
    constructed_base_urls: list[str] = []

    def factory(capabilities):
        constructed_base_urls.append(capabilities.base_url)
        return FakeBackend(detail=capabilities.base_url)

    client = TestClient(_app(backend_factory=factory), headers=AUTH_HEADERS)
    client.post("/v1/nodes/register", json=_register_payload())
    client.get("/health/ready")

    client.patch("/v1/nodes/host-a", json={"base_url": "http://new-host:1234"})
    response = client.get("/health/ready")

    assert response.json()["backends"][0]["detail"] == "http://new-host:1234"
    assert constructed_base_urls == ["http://localhost:1234", "http://new-host:1234"]


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


def test_a_registered_host_survives_an_app_restart():
    db_path = new_registry_db_path()
    first_client = TestClient(_app(db_path=db_path), headers=AUTH_HEADERS)
    first_client.post("/v1/nodes/register", json=_register_payload())

    second_client = TestClient(_app(db_path=db_path), headers=AUTH_HEADERS)

    assert [n["host_id"] for n in second_client.get("/v1/nodes").json()["nodes"]] == ["host-a"]


def test_health_ready_does_not_remove_a_host_regardless_of_time_elapsed():
    client = TestClient(_app(), headers=AUTH_HEADERS)
    client.post("/v1/nodes/register", json=_register_payload())

    client.get("/health/ready")

    assert [n["host_id"] for n in client.get("/v1/nodes").json()["nodes"]] == ["host-a"]


def test_a_freshly_registered_never_probed_host_reports_unknown_status():
    client = TestClient(_app(), headers=AUTH_HEADERS)

    client.post("/v1/nodes/register", json=_register_payload())

    nodes = client.get("/v1/nodes").json()["nodes"]
    assert nodes[0]["status"] == "unknown"


def test_a_host_that_passes_its_health_probe_reports_online_status():
    client = TestClient(_app(), headers=AUTH_HEADERS)
    client.post("/v1/nodes/register", json=_register_payload())

    client.get("/health/ready")

    nodes = client.get("/v1/nodes").json()["nodes"]
    assert nodes[0]["status"] == "online"


def test_a_host_that_fails_its_health_probe_reports_offline_status_and_stays_listed():
    class FailingBackend(FakeBackend):
        async def check_health(self):
            from llm_home_lab.backends.base import BackendHealth

            return BackendHealth(healthy=False, detail="down")

    client = TestClient(_app(backend_factory=lambda caps: FailingBackend()), headers=AUTH_HEADERS)
    client.post("/v1/nodes/register", json=_register_payload())

    for _ in range(3):
        client.get("/health/ready")

    nodes = client.get("/v1/nodes").json()["nodes"]
    assert nodes[0]["host_id"] == "host-a"
    assert nodes[0]["status"] == "offline"


def test_a_registered_host_reports_external_load_from_the_probe():
    async def create_subprocess(*args, **kwargs):
        class _Process:
            returncode = 0

            async def communicate(self):
                return b'[{"status": "processingPrompt", "queued": 2}]', b""

        return _Process()

    from llm_home_lab.registry.external_load import ExternalLoadProbe

    probe = ExternalLoadProbe(create_subprocess=create_subprocess)
    client = TestClient(_app(external_load_probe=probe), headers=AUTH_HEADERS)
    client.post("/v1/nodes/register", json=_register_payload())

    nodes = client.get("/v1/nodes").json()["nodes"]

    assert nodes[0]["external_load"] == {
        "available": True,
        "status": "processingPrompt",
        "queued": 2,
        "total_slots": None,
        "busy_slots": None,
    }


def test_a_host_with_no_working_lms_binary_reports_external_load_unavailable():
    nodes = TestClient(_app(), headers=AUTH_HEADERS)
    nodes.post("/v1/nodes/register", json=_register_payload())

    result = nodes.get("/v1/nodes").json()["nodes"]

    assert result[0]["external_load"] == {
        "available": False,
        "status": None,
        "queued": None,
        "total_slots": None,
        "busy_slots": None,
    }


class FailingBackend(FakeBackend):
    async def check_health(self):
        from llm_home_lab.backends.base import BackendHealth

        return BackendHealth(healthy=False, detail="down")


def test_capacity_sums_max_concurrent_requests_over_online_hosts_only():
    client = TestClient(
        _app(
            backend_factories={
                "ok": lambda caps: FakeBackend(),
                "fail": lambda caps: FailingBackend(),
            }
        ),
        headers=AUTH_HEADERS,
    )
    client.post(
        "/v1/nodes/register",
        json={**_register_payload("host-a"), "backend_type": "ok", "max_concurrent_requests": 4},
    )
    client.post(
        "/v1/nodes/register",
        json={**_register_payload("host-b"), "backend_type": "fail", "max_concurrent_requests": 6},
    )
    for _ in range(3):
        client.get("/health/ready")
    # Registered after the probes above, so it has no probe history yet — "unknown".
    client.post(
        "/v1/nodes/register",
        json={**_register_payload("host-c"), "backend_type": "ok", "max_concurrent_requests": 2},
    )

    body = client.get("/v1/capacity").json()

    assert body == {"total_max_concurrent_requests": 4}


def test_capacity_with_no_online_hosts_reports_zero_not_an_error():
    client = TestClient(_app(), headers=AUTH_HEADERS)

    response = client.get("/v1/capacity")

    assert response.status_code == 200
    assert response.json() == {"total_max_concurrent_requests": 0}


def test_capacity_from_an_online_zero_capacity_host_contributes_nothing():
    # Marks hosts online by writing straight into the HealthMonitor rather than via
    # /health/ready: that endpoint's metrics snapshot divides in_flight by
    # max_concurrent_requests per host (llm_home_lab/observability/metrics.py), which raises
    # ZeroDivisionError for a zero-capacity host — a pre-existing bug unrelated to /v1/capacity.
    health_monitor = HealthMonitor()
    client = TestClient(
        _app(backend_factories={"ok": lambda caps: FakeBackend()}, health_monitor=health_monitor),
        headers=AUTH_HEADERS,
    )
    client.post(
        "/v1/nodes/register",
        json={**_register_payload("host-d"), "backend_type": "ok", "max_concurrent_requests": 0},
    )
    client.post(
        "/v1/nodes/register",
        json={**_register_payload("host-e"), "backend_type": "ok", "max_concurrent_requests": 5},
    )
    health_monitor.record_probe("host-d", True, datetime.now(UTC))
    health_monitor.record_probe("host-e", True, datetime.now(UTC))

    body = client.get("/v1/capacity").json()

    assert body == {"total_max_concurrent_requests": 5}


def _llamaserver_probe(slots):
    import httpx

    from llm_home_lab.registry.llamaserver_load import LlamaCPPServerLoadProbe

    return LlamaCPPServerLoadProbe(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=slots))
    )


def test_a_llamaserver_host_reports_busy_and_total_slots_in_external_load():
    slots = [{"id": i, "is_processing": i < 7} for i in range(8)]
    client = TestClient(_app(external_load_probe=_llamaserver_probe(slots)), headers=AUTH_HEADERS)
    client.post("/v1/nodes/register", json=_register_payload())

    nodes = client.get("/v1/nodes").json()["nodes"]

    assert nodes[0]["external_load"] == {
        "available": True,
        "status": "busy",
        "queued": 7,
        "total_slots": 8,
        "busy_slots": 7,
    }


def test_a_host_with_no_completions_reports_zero_throughput_and_na_per_slot_without_slots():
    client = TestClient(_app(), headers=AUTH_HEADERS)
    client.post("/v1/nodes/register", json=_register_payload())

    nodes = client.get("/v1/nodes").json()["nodes"]

    assert nodes[0]["throughput"] == {
        "window_s": 3600.0,
        "tasks_per_hour": 0.0,
        "tasks_per_hour_per_slot": None,
    }


def test_throughput_per_slot_is_reported_for_a_host_with_a_known_slot_count():
    from llm_home_lab.observability.completion_log import CompletionLog

    slots = [{"id": i, "is_processing": False} for i in range(4)]
    log = CompletionLog(clock=lambda: datetime.now(UTC) - timedelta(hours=1))
    for _ in range(8):
        log.record("host-a", datetime.now(UTC))
    app = _app(external_load_probe=_llamaserver_probe(slots), completion_log=log)
    client = TestClient(app, headers=AUTH_HEADERS)
    client.post("/v1/nodes/register", json=_register_payload())

    nodes = client.get("/v1/nodes").json()["nodes"]

    assert nodes[0]["throughput"]["tasks_per_hour"] == 8.0
    assert nodes[0]["throughput"]["tasks_per_hour_per_slot"] == 2.0

import asyncio
import threading
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from registry_test_helpers import inert_external_load_probe, new_registry_db_path

from llm_home_lab.api.app import create_app
from llm_home_lab.backends.base import BackendChunk, BackendResponse
from llm_home_lab.health.monitor import HealthMonitor
from llm_home_lab.leases.book import NodeLeaseBook
from llm_home_lab.leases.models import LEASE_HEADER_NAME, LeaseSettings, ReservableHost
from llm_home_lab.observability.alerts import AlertEvaluator
from llm_home_lab.observability.metrics import MetricsRegistry
from llm_home_lab.registry.registry import HostRegistry
from llm_home_lab.routing.engine import RoutingEngine
from llm_home_lab.routing.models import PolicyRule, RoutingPolicy
from llm_home_lab.scheduling.queue import SchedulingQueue
from llm_home_lab.security.key_store import ApiKeyStore
from llm_home_lab.security.models import ApiKey, ClientConfig

AUTH_HEADERS = {"Authorization": "Bearer test-key"}
T0 = datetime(2026, 10, 25, 10, 0, tzinfo=UTC)


def _key_store() -> ApiKeyStore:
    return ApiKeyStore(
        [
            ClientConfig(
                client_id="test-client",
                allowed_path_prefixes=["/"],
                keys=[ApiKey(key="test-key", expires_at=None)],
            )
        ]
    )


class RecordingBackend:
    def __init__(self) -> None:
        self.backend_id = "unset"
        self.calls: list[str] = []

    async def complete(self, request):
        self.calls.append(self.backend_id)
        return BackendResponse(
            model=request.model,
            content=self.backend_id,
            finish_reason="stop",
            prompt_tokens=1,
            completion_tokens=1,
        )

    async def stream(self, request):
        self.calls.append(self.backend_id)
        yield BackendChunk(content="ok", finish_reason="stop")


class GateBackend(RecordingBackend):
    def __init__(self, started: threading.Event, release: threading.Event) -> None:
        super().__init__()
        self._started = started
        self._release = release

    async def complete(self, request):
        self._started.set()
        while not self._release.is_set():
            await asyncio.sleep(0.01)
        return await super().complete(request)


def _settings(**overrides: object) -> LeaseSettings:
    values: dict[str, object] = {
        "ttl": timedelta(seconds=30),
        "default_ceiling": timedelta(hours=3),
        "max_ceiling": timedelta(hours=6),
        "min_active_nodes": 1,
        "expiring_soon": timedelta(seconds=10),
    }
    values.update(overrides)
    return LeaseSettings(**values)  # type: ignore[arg-type]


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _app(
    *,
    settings: LeaseSettings | None = None,
    clock: _Clock | None = None,
    backend_factory=None,
    dispatch_wait_timeout: float = 0.2,
):
    return create_app(
        registry=HostRegistry(new_registry_db_path()),
        router=RoutingEngine(
            RoutingPolicy(rules=[PolicyRule(name="flat", score_fn=lambda c, ctx: 0.0)])
        ),
        health_monitor=HealthMonitor(),
        scheduling_queue=SchedulingQueue(),
        backend_factories={"fake": backend_factory or (lambda caps: RecordingBackend())},
        metrics_registry=MetricsRegistry(),
        alert_evaluator=AlertEvaluator([]),
        key_store=_key_store(),
        external_load_probe=inert_external_load_probe(),
        dispatch_wait_timeout=dispatch_wait_timeout,
        dispatch_poll_interval=0.01,
        lease_settings=settings or _settings(),
        lease_clock=clock,
    )


def _register(client: TestClient, host_id: str, **extra: object) -> None:
    payload: dict[str, object] = {
        "host_id": host_id,
        "backend_type": "fake",
        "context_window": 8192,
        "base_url": "http://fake",
        "max_concurrent_requests": 4,
    }
    payload.update(extra)
    response = client.post("/v1/nodes/register", json=payload)
    assert response.status_code == 200


def _chat(model: str = "gemma") -> dict[str, object]:
    return {"model": model, "messages": [{"role": "user", "content": "Hi"}], "stream": False}


def _host(host_id: str, *, healthy: bool = True, serves: bool = True, status: str = "online"):
    return ReservableHost(
        host_id=host_id,
        status=status,
        healthy=healthy,
        serves_requested_model=serves,
    )


def test_a_short_lease_is_refused_for_the_last_active_node_and_names_the_roster():
    book = NodeLeaseBook(_settings(min_active_nodes=1))
    hosts = [_host("host-a"), _host("host-b", healthy=False, status="offline")]

    try:
        book.reserve(holder="ada", hosts=hosts, host_id=None, model=None, ceiling=None, now=T0)
        raise AssertionError("expected refusal")
    except Exception as exc:
        message = str(exc)

    assert "minimum of 1" in message
    assert "host-a status=online holder=free" in message
    assert "host-b status=offline holder=free" in message


def test_an_explicit_second_reservation_names_the_holder_and_expiry():
    book = NodeLeaseBook(_settings())
    hosts = [_host("host-a"), _host("host-b")]
    first = book.reserve(
        holder="ada", hosts=hosts, host_id="host-a", model=None, ceiling=None, now=T0
    )

    try:
        book.reserve(holder="bea", hosts=hosts, host_id="host-a", model=None, ceiling=None, now=T0)
        raise AssertionError("expected refusal")
    except Exception as exc:
        message = str(exc)

    assert "already reserved by ada" in message
    assert first.expires_at.isoformat() in message
    assert "host-b status=online holder=free" in message


def test_a_missed_heartbeat_drops_the_lease_within_one_ttl():
    book = NodeLeaseBook(_settings(ttl=timedelta(seconds=30)))
    hosts = [_host("host-a"), _host("host-b")]
    lease = book.reserve(
        holder="ada", hosts=hosts, host_id="host-a", model=None, ceiling=None, now=T0
    )

    renewed = book.heartbeat(lease.token, T0 + timedelta(seconds=20))
    assert renewed.expires_at == T0 + timedelta(seconds=50)
    assert book.is_held("host-a", T0 + timedelta(seconds=49))
    assert not book.is_held("host-a", T0 + timedelta(seconds=50))


def test_a_ceiling_above_the_server_maximum_is_rejected_and_a_shorter_one_is_kept():
    book = NodeLeaseBook(
        _settings(max_ceiling=timedelta(hours=6), default_ceiling=timedelta(hours=3))
    )
    hosts = [_host("host-a"), _host("host-b")]

    try:
        book.reserve(
            holder="ada",
            hosts=hosts,
            host_id="host-a",
            model=None,
            ceiling=timedelta(hours=7),
            now=T0,
        )
        raise AssertionError("expected invalid ceiling")
    except Exception as exc:
        assert "server maximum" in str(exc)

    lease = book.reserve(
        holder="ada",
        hosts=hosts,
        host_id="host-a",
        model=None,
        ceiling=timedelta(seconds=40),
        now=T0,
    )
    assert lease.hard_expires_at == T0 + timedelta(seconds=40)
    assert lease.expires_at == T0 + timedelta(seconds=30)
    renewed = book.heartbeat(lease.token, T0 + timedelta(seconds=20))
    assert renewed.expires_at == lease.hard_expires_at
    try:
        book.heartbeat(lease.token, T0 + timedelta(seconds=40))
        raise AssertionError("expected the ceiling to end the lease")
    except Exception as exc:
        assert "ended" in str(exc)
    assert not book.is_held("host-a", T0 + timedelta(seconds=40))


def test_reservation_routing_and_force_release_stay_on_the_orchestrator():
    clock = _Clock(T0)
    client = TestClient(_app(clock=clock), headers=AUTH_HEADERS)
    _register(client, "host-a", allowed_models=["gemma"])
    _register(client, "host-b", allowed_models=["gemma", "other"])

    refused = client.post("/v1/node-leases", json={"holder": "ada", "host_id": "missing"})
    assert refused.status_code == 404

    created = client.post(
        "/v1/node-leases",
        json={"holder": "ada", "host_id": "host-a", "model": "gemma"},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["host_id"] == "host-a"
    assert body["holder"] == "ada"
    assert "token" in body
    token = body["token"]

    again = client.post("/v1/node-leases", json={"holder": "bea", "host_id": "host-a"})
    assert again.status_code == 409
    assert "ada" in again.json()["error"]["message"]
    assert "until" in again.json()["error"]["message"]
    assert "host-b" in again.json()["error"]["message"]

    nodes = client.get("/v1/nodes").json()["nodes"]
    leased = next(node for node in nodes if node["host_id"] == "host-a")
    assert leased["lease"]["holder"] == "ada"
    assert leased["lease"]["expiring_soon"] is False
    assert "token" not in leased["lease"]
    assert next(node for node in nodes if node["host_id"] == "host-b")["lease"] is None

    ordinary = client.post("/v1/chat/completions", json=_chat("gemma"))
    assert ordinary.status_code == 200
    assert ordinary.headers["X-Backend-Id"] == "host-b"

    pinned = client.post(
        "/v1/chat/completions",
        json=_chat("gemma"),
        headers={LEASE_HEADER_NAME: token},
    )
    assert pinned.status_code == 200
    assert pinned.headers["X-Backend-Id"] == "host-a"

    wrong_model = client.post(
        "/v1/chat/completions",
        json=_chat("other"),
        headers={LEASE_HEADER_NAME: token},
    )
    assert wrong_model.status_code == 400
    assert "host-a" in wrong_model.json()["error"]["message"]

    stolen = client.delete("/v1/node-leases", headers={LEASE_HEADER_NAME: "nope"})
    assert stolen.status_code == 403

    clock.now = T0 + timedelta(seconds=10)
    beat = client.post("/v1/node-leases/heartbeat", headers={LEASE_HEADER_NAME: token})
    assert beat.status_code == 200
    assert beat.json()["expires_at"] != body["expires_at"]

    forced = client.post("/v1/node-leases/force-release", json={"host_id": "host-a"})
    assert forced.status_code == 200
    assert client.get("/v1/nodes").json()["nodes"][0]["lease"] is None

    after = client.post("/v1/chat/completions", json=_chat("gemma"))
    assert after.status_code == 200
    assert after.headers["X-Backend-Id"] == "host-a"


def test_an_in_flight_request_finishes_after_its_node_is_reserved():
    started = threading.Event()
    release = threading.Event()
    client = TestClient(
        _app(backend_factory=lambda caps: GateBackend(started, release), settings=_settings()),
        headers=AUTH_HEADERS,
    )
    _register(client, "host-a")
    _register(client, "host-b")
    result: dict[str, object] = {}

    def call() -> None:
        result["response"] = client.post("/v1/chat/completions", json=_chat())

    worker = threading.Thread(target=call)
    worker.start()
    assert started.wait(5)

    created = client.post("/v1/node-leases", json={"holder": "ada", "host_id": "host-a"})
    assert created.status_code == 201
    release.set()
    worker.join(5)

    response = result["response"]
    assert response.status_code == 200
    assert response.headers["X-Backend-Id"] == "host-a"

    ordinary = client.post("/v1/chat/completions", json=_chat())
    assert ordinary.headers["X-Backend-Id"] == "host-b"


def test_auto_reserve_picks_a_model_match_and_refuses_when_none_are_free():
    client = TestClient(_app(), headers=AUTH_HEADERS)
    _register(client, "host-b", allowed_models=["other"])
    _register(client, "host-a", allowed_models=["gemma"])

    created = client.post("/v1/node-leases", json={"holder": "ada", "model": "gemma"})
    assert created.status_code == 201
    assert created.json()["host_id"] == "host-a"

    blocked = client.post("/v1/node-leases", json={"holder": "bea", "model": "gemma"})
    assert blocked.status_code == 409
    message = blocked.json()["error"]["message"]
    assert "host-a" in message
    assert "ada" in message
    assert "holder=free" in message or "host-b" in message


def test_the_default_ceiling_is_three_hours_and_the_env_can_raise_the_maximum():
    from llm_home_lab.leases.settings import lease_settings_from_env

    defaults = lease_settings_from_env({})
    assert defaults.ttl == timedelta(minutes=3)
    assert defaults.default_ceiling == timedelta(hours=3)
    assert defaults.max_ceiling == timedelta(hours=3)
    assert defaults.min_active_nodes == 1

    raised = lease_settings_from_env(
        {
            "ORCHESTRATOR_LEASE_CEILING_S": "7200",
            "ORCHESTRATOR_LEASE_MAX_CEILING_S": "14400",
            "ORCHESTRATOR_LEASE_MIN_ACTIVE_NODES": "2",
        }
    )
    assert raised.default_ceiling == timedelta(hours=2)
    assert raised.max_ceiling == timedelta(hours=4)
    assert raised.min_active_nodes == 2


async def test_queued_ordinary_request_rechecks_reservations_and_keeps_its_entry():
    from httpx import ASGITransport, AsyncClient

    app = _app(dispatch_wait_timeout=2)
    with TestClient(app, headers=AUTH_HEADERS) as setup:
        _register(setup, "host-a", max_concurrent_requests=1)
        _register(setup, "host-b", max_concurrent_requests=1)
    registry = app.state.registry
    queue = app.state.scheduling_queue
    registry.acquire_slot("host-a")
    registry.acquire_slot("host-b")
    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://test", headers=AUTH_HEADERS
    ) as client:
        ordinary = asyncio.create_task(client.post("/v1/chat/completions", json=_chat()))
        for _ in range(100):
            if queue.depth() == 1:
                break
            await asyncio.sleep(0.01)
        assert queue.depth() == 1
        lease = await client.post("/v1/node-leases", json={"holder": "ada", "host_id": "host-a"})
        assert lease.status_code == 201
        pinned = asyncio.create_task(
            client.post(
                "/v1/chat/completions",
                json=_chat(),
                headers={LEASE_HEADER_NAME: lease.json()["token"]},
            )
        )
        for _ in range(100):
            if queue.depth() == 2:
                break
            await asyncio.sleep(0.01)
        assert queue.depth() == 2
        registry.release_slot("host-a")
        result = await pinned
        assert result.status_code == 200
        assert result.headers["X-Backend-Id"] == "host-a"
        assert not ordinary.done()
        assert queue.depth() == 1
        registry.release_slot("host-b")
        result = await ordinary
        assert result.status_code == 200
        assert result.headers["X-Backend-Id"] == "host-b"
        assert queue.depth() == 0


async def test_queued_pinned_request_rejects_expired_or_replaced_lease():
    from httpx import ASGITransport, AsyncClient

    for expire in (True, False):
        clock = _Clock(T0)
        app = _app(clock=clock, dispatch_wait_timeout=2)
        with TestClient(app, headers=AUTH_HEADERS) as setup:
            _register(setup, "host-a", max_concurrent_requests=1)
            _register(setup, "host-b", max_concurrent_requests=1)
        registry = app.state.registry
        registry.acquire_slot("host-a")
        async with AsyncClient(
            transport=ASGITransport(app), base_url="http://test", headers=AUTH_HEADERS
        ) as client:
            lease = await client.post(
                "/v1/node-leases", json={"holder": "ada", "host_id": "host-a"}
            )
            pending = asyncio.create_task(
                client.post(
                    "/v1/chat/completions",
                    json=_chat(),
                    headers={LEASE_HEADER_NAME: lease.json()["token"]},
                )
            )
            for _ in range(100):
                if app.state.scheduling_queue.depth() == 1:
                    break
                await asyncio.sleep(0.01)
            assert app.state.scheduling_queue.depth() == 1
            if expire:
                clock.now += timedelta(seconds=31)
            else:
                released = await client.post(
                    "/v1/node-leases/force-release", json={"host_id": "host-a"}
                )
                assert released.status_code == 200
            replacement = await client.post(
                "/v1/node-leases", json={"holder": "bea", "host_id": "host-a"}
            )
            assert replacement.status_code == 201
            registry.release_slot("host-a")
            result = await pending
            assert result.status_code in (403, 409)
            assert registry.in_flight("host-a") == 0
            assert app.state.scheduling_queue.depth() == 0

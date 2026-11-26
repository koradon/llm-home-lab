import asyncio
import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from registry_test_helpers import inert_external_load_probe, new_registry_db_path

from llm_home_lab.api.app import _stream_chunks, create_app
from llm_home_lab.api.models import ChatCompletionRequest
from llm_home_lab.backends.base import (
    BackendChunk,
    BackendConnectionError,
    BackendHealth,
    BackendResponse,
    BackendResponseError,
    RouterModelState,
)
from llm_home_lab.backends.llamaserver import LlamaCPPServerBackend
from llm_home_lab.health.monitor import HealthMonitor
from llm_home_lab.leases.models import LeaseSettings
from llm_home_lab.observability.alerts import AlertEvaluator
from llm_home_lab.observability.completion_log import CompletionLog
from llm_home_lab.observability.metrics import MetricsRegistry
from llm_home_lab.registry.llamaserver_load import LlamaCPPServerLoadProbe
from llm_home_lab.registry.multi_backend_load_probe import MultiBackendLoadProbe
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


class _FakeRouterServer:
    """Loopback HTTP server emulating llama-server in router mode (--models-max 1)."""

    def __init__(
        self,
        *,
        models: dict[str, dict[str, object]],
        slots_by_model: dict[str, list[dict[str, object]]] | None = None,
        broken_models: set[str] | None = None,
        health_ok: bool = True,
    ) -> None:
        self.models = {k: dict(v) for k, v in models.items()}
        self.slots_by_model = dict(slots_by_model or {})
        self.broken_models = set(broken_models or set())
        self.health_ok = health_ok
        self.requests: list[tuple[str, str]] = []
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def __enter__(self) -> "_FakeRouterServer":
        self._thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def _send_json(self, status_code: int, payload: object) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                with outer._lock:
                    outer.requests.append((self.command, self.path))
                    if parsed.path == "/health":
                        if outer.health_ok:
                            self._send_json(200, {"status": "ok"})
                        else:
                            self._send_json(503, {"error": {"message": "down"}})
                        return

                    if parsed.path == "/models":
                        data = [
                            {"id": model_id, "object": "model", "status": dict(status)}
                            for model_id, status in outer.models.items()
                        ]
                        self._send_json(200, {"object": "list", "data": data})
                        return

                    if parsed.path == "/v1/models":
                        # Mirrors [M-14]: /v1/models lists all entries including unloaded/failed.
                        data = [{"id": model_id, "object": "model"} for model_id in outer.models]
                        self._send_json(200, {"object": "list", "data": data})
                        return

                    if parsed.path == "/slots":
                        query = parse_qs(parsed.query)
                        model_names = query.get("model")
                        if not model_names:
                            # Mirrors [M-14]: parameterless /slots returns 400 on router mode.
                            self._send_json(
                                400,
                                {"error": {"message": "model name is missing from the request"}},
                            )
                            return
                        model_name = model_names[0]
                        slots = outer.slots_by_model.get(model_name)
                        if slots is None:
                            self._send_json(
                                400,
                                {"error": {"message": f"model {model_name!r} is not loaded"}},
                            )
                            return
                        self._send_json(200, slots)
                        return

                    self._send_json(404, {"error": {"message": "not found"}})

            def do_POST(self) -> None:
                parsed = urlparse(self.path)
                length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(length) if length else b"{}"
                with outer._lock:
                    outer.requests.append((self.command, self.path))
                    if parsed.path != "/v1/chat/completions":
                        self._send_json(404, {"error": {"message": "not found"}})
                        return
                    payload = json.loads(raw_body)
                    model = payload.get("model", "")
                    if model in outer.broken_models:
                        outer.models[model] = {
                            "value": "unloaded",
                            "failed": True,
                            "exit_code": 1,
                        }
                        self._send_json(
                            500, {"error": {"message": f"model name={model} failed to load"}}
                        )
                        return
                    status = outer.models.get(model)
                    if status is not None and status.get("value") == "loading":
                        self._send_json(503, {"error": {"message": "Loading model"}})
                        return

                sse = (
                    b'data: {"choices": [{"delta": {"content": "ok"}, "finish_reason": null}]}\n\n'
                    b'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}\n\n'
                    b'data: {"choices": [], '
                    b'"usage": {"prompt_tokens": 3, "completion_tokens": 1}}\n\n'
                    b"data: [DONE]\n\n"
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(sse)))
                self.end_headers()
                self.wfile.write(sse)

        return Handler


class _FakeSingleModelServer:
    """Loopback HTTP server emulating single-model (non-router) llama-server."""

    def __init__(self, *, model_id: str, fail_completions: bool = False) -> None:
        self.model_id = model_id
        self.fail_completions = fail_completions
        self.requests: list[tuple[str, str]] = []
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def __enter__(self) -> "_FakeSingleModelServer":
        self._thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def _send_json(self, status_code: int, payload: object) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                with outer._lock:
                    outer.requests.append((self.command, self.path))
                    if parsed.path == "/health":
                        self._send_json(200, {"status": "ok"})
                        return
                    if parsed.path == "/v1/models":
                        self._send_json(200, {"object": "list", "data": [{"id": outer.model_id}]})
                        return
                    if parsed.path == "/slots":
                        self._send_json(
                            200,
                            [
                                {"id": 0, "is_processing": True},
                                {"id": 1, "is_processing": False},
                            ],
                        )
                        return
                    self._send_json(404, {"error": {"message": "not found"}})

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                if length:
                    self.rfile.read(length)
                with outer._lock:
                    outer.requests.append((self.command, self.path))
                    if outer.fail_completions:
                        self._send_json(500, {"error": {"message": "boom"}})
                        return
                sse = (
                    b'data: {"choices": [{"delta": {"content": "ok"}, '
                    b'"finish_reason": "stop"}]}\n\n'
                    b'data: {"choices": [], '
                    b'"usage": {"prompt_tokens": 2, "completion_tokens": 1}}\n\n'
                    b"data: [DONE]\n\n"
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(sse)))
                self.end_headers()
                self.wfile.write(sse)

        return Handler


def _build_llamaserver_app(
    *,
    db_path: str | None = None,
    health_monitor: HealthMonitor | None = None,
    completion_log: CompletionLog | None = None,
    dispatch_wait_timeout: float = 0.2,
    cache_ttl: timedelta = timedelta(seconds=0),
):
    registry = HostRegistry(db_path or new_registry_db_path())
    policy = RoutingPolicy(rules=[PolicyRule(name="flat", score_fn=lambda c, ctx: -c.latency_ms)])
    return create_app(
        registry=registry,
        router=RoutingEngine(policy),
        health_monitor=health_monitor or HealthMonitor(),
        scheduling_queue=SchedulingQueue(),
        backend_factories={
            "llamaserver": lambda caps: LlamaCPPServerBackend(
                base_url=caps.base_url,
                timeout=5.0,
                max_retries=0,
                swap_mode=caps.swap_mode,
            )
        },
        metrics_registry=MetricsRegistry(),
        alert_evaluator=AlertEvaluator([]),
        key_store=_permissive_key_store(),
        external_load_probe=MultiBackendLoadProbe(
            registry=registry,
            probes_by_backend_type={"llamaserver": LlamaCPPServerLoadProbe(cache_ttl=cache_ttl)},
        ),
        completion_log=completion_log,
        lease_settings=LeaseSettings(min_active_nodes=0),
        dispatch_wait_timeout=dispatch_wait_timeout,
        dispatch_poll_interval=0.01,
    )


def _node_payload(host_id: str, base_url: str, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "host_id": host_id,
        "backend_type": "llamaserver",
        "context_window": 8192,
        "base_url": base_url,
        "max_concurrent_requests": 4,
    }
    return payload | overrides


def test_router_probe_restores_slot_telemetry_and_tasks_per_hour_per_slot():
    with _FakeRouterServer(
        models={
            "hy-mt2": {"value": "loaded"},
            "tgemma": {"value": "unloaded"},
        },
        slots_by_model={
            "hy-mt2": [
                {"id": 0, "n_ctx": 2048, "is_processing": True},
                {"id": 1, "n_ctx": 2048, "is_processing": False},
                {"id": 2, "n_ctx": 2048, "is_processing": False},
                {"id": 3, "n_ctx": 2048, "is_processing": False},
            ]
        },
    ) as router:
        log = CompletionLog(clock=lambda: datetime.now(UTC) - timedelta(hours=1))
        for _ in range(12):
            log.record("mini-1", datetime.now(UTC))
        client = TestClient(_build_llamaserver_app(completion_log=log), headers=AUTH_HEADERS)
        client.post(
            "/v1/nodes/register",
            json=_node_payload(
                "mini-1",
                router.base_url,
                swap_mode="router",
                allowed_models=["hy-mt2"],
            ),
        )

        ready = client.get("/health/ready")

        assert ready.status_code == 200
        node = client.get("/v1/nodes").json()["nodes"][0]
        assert node["swap_mode"] == "router"
        assert node["external_load"] == {
            "available": True,
            "status": "busy",
            "queued": 1,
            "total_slots": 4,
            "busy_slots": 1,
        }
        assert node["throughput"]["tasks_per_hour"] == 12.0
        assert node["throughput"]["tasks_per_hour_per_slot"] == 3.0
        assert node["models"] == [
            {"id": "hy-mt2", "state": "loaded", "healthy": True, "recent_failures": 0},
            {"id": "tgemma", "state": "unloaded", "healthy": True, "recent_failures": 0},
        ]
        assert ("GET", "/slots") not in router.requests
        assert ("GET", "/slots?model=hy-mt2") in router.requests


def test_router_probe_skips_parameterless_slots_when_no_model_is_loaded():
    with _FakeRouterServer(
        models={
            "hy-mt2": {"value": "unloaded"},
            "tgemma": {"value": "loading"},
        }
    ) as router:
        client = TestClient(_build_llamaserver_app(), headers=AUTH_HEADERS)
        client.post(
            "/v1/nodes/register",
            json=_node_payload("mini-1", router.base_url, swap_mode="router"),
        )

        client.get("/health/ready")

        node = client.get("/v1/nodes").json()["nodes"][0]
        assert node["external_load"]["available"] is False
        assert not any(path.startswith("/slots") for _, path in router.requests)


def test_router_probe_uses_sleeping_model_when_no_model_is_loaded():
    with _FakeRouterServer(
        models={"hy-mt2": {"value": "sleeping"}},
        slots_by_model={
            "hy-mt2": [{"id": 0, "is_processing": False}, {"id": 1, "is_processing": False}]
        },
    ) as router:
        client = TestClient(_build_llamaserver_app(), headers=AUTH_HEADERS)
        client.post(
            "/v1/nodes/register",
            json=_node_payload("mini-1", router.base_url, swap_mode="router"),
        )

        client.get("/health/ready")

        node = client.get("/v1/nodes").json()["nodes"][0]
        assert node["external_load"]["available"] is True
        assert node["external_load"]["total_slots"] == 2
        assert ("GET", "/slots?model=hy-mt2") in router.requests


def test_router_probe_updates_slot_telemetry_after_model_switch():
    with _FakeRouterServer(
        models={"hy-mt2": {"value": "loaded"}, "tgemma": {"value": "unloaded"}},
        slots_by_model={
            "hy-mt2": [{"id": i, "is_processing": False} for i in range(2)],
            "tgemma": [{"id": i, "is_processing": False} for i in range(6)],
        },
    ) as router:
        client = TestClient(
            _build_llamaserver_app(cache_ttl=timedelta(seconds=60)), headers=AUTH_HEADERS
        )
        client.post(
            "/v1/nodes/register",
            json=_node_payload("mini-1", router.base_url, swap_mode="router"),
        )
        client.get("/health/ready")
        router.models["hy-mt2"] = {"value": "unloaded"}
        router.models["tgemma"] = {"value": "loaded"}

        client.get("/health/ready")

        node = client.get("/v1/nodes").json()["nodes"][0]
        assert node["external_load"]["total_slots"] == 6
        assert ("GET", "/slots?model=tgemma") in router.requests


def test_broken_model_does_not_take_router_node_down_and_zeros_only_its_capacity():
    with _FakeRouterServer(
        models={
            "broken-missing": {"value": "unloaded"},
            "hy-mt2": {"value": "loaded"},
        },
        slots_by_model={"hy-mt2": [{"id": 0, "is_processing": False}]},
        broken_models={"broken-missing"},
    ) as router:
        client = TestClient(_build_llamaserver_app(), headers=AUTH_HEADERS)
        client.post(
            "/v1/nodes/register",
            json=_node_payload(
                "mini-1",
                router.base_url,
                swap_mode="router",
                max_concurrent_requests=4,
                allowed_models=["broken-missing", "hy-mt2"],
                model_limits={"hy-mt2": {"max_concurrent_requests": 8}},
            ),
        )
        client.get("/health/ready")

        broken_statuses = [
            client.post(
                "/v1/chat/completions",
                json={
                    "model": "broken-missing",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            ).status_code
            for _ in range(3)
        ]

        assert broken_statuses == [503, 503, 503]
        node = client.get("/v1/nodes").json()["nodes"][0]
        assert node["status"] == "online"
        assert node["health"]["recent_failures"] == 0
        models_by_id = {m["id"]: m for m in node["models"]}
        assert models_by_id["broken-missing"]["healthy"] is False
        assert models_by_id["broken-missing"]["recent_failures"] == 3
        assert models_by_id["hy-mt2"]["healthy"] is True
        assert client.get("/v1/capacity", params={"model": "broken-missing"}).json() == {
            "total_max_concurrent_requests": 0
        }
        assert client.get("/v1/capacity", params={"model": "hy-mt2"}).json() == {
            "total_max_concurrent_requests": 8
        }
        good_res = client.post(
            "/v1/chat/completions",
            json={"model": "hy-mt2", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert good_res.status_code == 200


def test_unhealthy_model_recovers_via_half_open_trial_after_cooldown():
    with _FakeRouterServer(
        models={
            "broken-missing": {"value": "unloaded"},
            "hy-mt2": {"value": "loaded"},
        },
        slots_by_model={
            "broken-missing": [{"id": 0, "is_processing": False}],
            "hy-mt2": [{"id": 0, "is_processing": False}],
        },
        broken_models={"broken-missing"},
    ) as router:
        health_monitor = HealthMonitor(
            failure_threshold=3, recovery_threshold=2, cooldown=timedelta(seconds=0)
        )
        client = TestClient(
            _build_llamaserver_app(health_monitor=health_monitor), headers=AUTH_HEADERS
        )
        client.post(
            "/v1/nodes/register",
            json=_node_payload(
                "mini-1",
                router.base_url,
                swap_mode="router",
                max_concurrent_requests=4,
                allowed_models=["broken-missing", "hy-mt2"],
            ),
        )
        client.get("/health/ready")

        for _ in range(3):
            assert (
                client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "broken-missing",
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                ).status_code
                == 503
            )

        assert client.get("/v1/capacity", params={"model": "broken-missing"}).json() == {
            "total_max_concurrent_requests": 0
        }

        router.broken_models.discard("broken-missing")
        router.models["broken-missing"] = {"value": "loaded", "failed": False, "exit_code": 0}

        assert client.get("/v1/capacity", params={"model": "broken-missing"}).json() == {
            "total_max_concurrent_requests": 0
        }
        recovered = client.post(
            "/v1/chat/completions",
            json={
                "model": "broken-missing",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert recovered.status_code == 200
        assert client.get("/v1/capacity", params={"model": "broken-missing"}).json() == {
            "total_max_concurrent_requests": 4
        }
        assert health_monitor.is_model_healthy("mini-1", "broken-missing", datetime.now(UTC))


def test_half_open_model_failed_trial_reopens_cooldown_and_keeps_capacity_zero():
    with _FakeRouterServer(
        models={"broken-missing": {"value": "unloaded"}},
        slots_by_model={"broken-missing": [{"id": 0, "is_processing": False}]},
        broken_models={"broken-missing"},
    ) as router:
        health_monitor = HealthMonitor(
            failure_threshold=3, recovery_threshold=2, cooldown=timedelta(seconds=30)
        )
        client = TestClient(
            _build_llamaserver_app(health_monitor=health_monitor), headers=AUTH_HEADERS
        )
        client.post(
            "/v1/nodes/register",
            json=_node_payload(
                "mini-1",
                router.base_url,
                swap_mode="router",
                allowed_models=["broken-missing"],
            ),
        )
        client.get("/health/ready")

        for _ in range(3):
            assert (
                client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "broken-missing",
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                ).status_code
                == 503
            )

        opened_at = datetime.now(UTC)
        assert health_monitor.may_admit_model("mini-1", "broken-missing", opened_at) is False
        # Elapse cooldown without a poller so the next request is the half-open trial.
        health_monitor._model_states[("mini-1", "broken-missing")].unhealthy_since = (
            opened_at - timedelta(seconds=30)
        )

        assert client.get("/v1/capacity", params={"model": "broken-missing"}).json() == {
            "total_max_concurrent_requests": 0
        }
        trial = client.post(
            "/v1/chat/completions",
            json={
                "model": "broken-missing",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert trial.status_code == 503
        assert client.get("/v1/capacity", params={"model": "broken-missing"}).json() == {
            "total_max_concurrent_requests": 0
        }
        now = datetime.now(UTC)
        assert health_monitor.is_model_healthy("mini-1", "broken-missing", now) is False
        assert health_monitor.has_model_trial("mini-1", "broken-missing") is False
        assert health_monitor.may_admit_model("mini-1", "broken-missing", now) is False
        assert (
            health_monitor.may_admit_model(
                "mini-1", "broken-missing", now + timedelta(seconds=29)
            )
            is False
        )


def test_router_reported_failed_model_zeros_capacity_until_recovery():
    with _FakeRouterServer(
        models={
            "broken-model": {"value": "unloaded", "failed": True, "exit_code": 1},
            "hy-mt2": {"value": "loaded"},
        },
        slots_by_model={"hy-mt2": [{"id": 0, "is_processing": False}]},
    ) as router:
        health_monitor = HealthMonitor(
            failure_threshold=3, recovery_threshold=1, cooldown=timedelta(seconds=0)
        )
        client = TestClient(
            _build_llamaserver_app(health_monitor=health_monitor), headers=AUTH_HEADERS
        )
        client.post(
            "/v1/nodes/register",
            json=_node_payload(
                "mini-1",
                router.base_url,
                swap_mode="router",
                allowed_models=["broken-model", "hy-mt2"],
            ),
        )

        client.get("/health/ready")

        assert client.get("/v1/capacity", params={"model": "broken-model"}).json() == {
            "total_max_concurrent_requests": 0
        }
        assert client.get("/v1/capacity", params={"model": "hy-mt2"}).json() == {
            "total_max_concurrent_requests": 4
        }
        node = client.get("/v1/nodes").json()["nodes"][0]
        assert node["status"] == "online"
        models_by_id = {m["id"]: m for m in node["models"]}
        assert models_by_id["broken-model"]["state"] == "failed"
        assert models_by_id["broken-model"]["healthy"] is False

        router.models["broken-model"] = {"value": "loaded", "failed": False, "exit_code": 0}
        router.slots_by_model["broken-model"] = [{"id": 0, "is_processing": False}]

        client.get("/health/ready")

        assert client.get("/v1/capacity", params={"model": "broken-model"}).json() == {
            "total_max_concurrent_requests": 4
        }


def test_request_for_loading_model_returns_503_model_loading_with_retry_after():
    with _FakeRouterServer(
        models={"hy-mt2": {"value": "loading"}},
    ) as router:
        client = TestClient(_build_llamaserver_app(), headers=AUTH_HEADERS)
        client.post(
            "/v1/nodes/register",
            json=_node_payload(
                "mini-1",
                router.base_url,
                swap_mode="router",
                allowed_models=["hy-mt2"],
            ),
        )
        client.get("/health/ready")

        response = client.post(
            "/v1/chat/completions",
            json={"model": "hy-mt2", "messages": [{"role": "user", "content": "hi"}]},
        )

        assert response.status_code == 503
        assert response.json()["error"]["code"] == "model_loading"
        assert response.json()["error"]["type"] == "backend_error"
        assert int(response.headers["Retry-After"]) > 0
        node = client.get("/v1/nodes").json()["nodes"][0]
        assert node["status"] == "online"
        assert node["health"]["recent_failures"] == 0
        assert node["models"][0]["recent_failures"] == 0


def test_unrestricted_router_node_returns_503_model_loading_instead_of_400():
    with _FakeRouterServer(
        models={"hy-mt2": {"value": "loading"}, "other": {"value": "unloaded"}},
    ) as router:
        client = TestClient(_build_llamaserver_app(), headers=AUTH_HEADERS)
        client.post(
            "/v1/nodes/register",
            json=_node_payload("mini-1", router.base_url, swap_mode="router"),
        )
        client.get("/health/ready")

        response = client.post(
            "/v1/chat/completions",
            json={"model": "hy-mt2", "messages": [{"role": "user", "content": "hi"}]},
        )

        assert response.status_code == 503
        assert response.json()["error"]["code"] == "model_loading"
        assert int(response.headers["Retry-After"]) > 0


def test_backend_503_loading_response_on_router_maps_to_model_loading_without_failure():
    with _FakeRouterServer(
        models={"hy-mt2": {"value": "loading"}},
    ) as router:
        client = TestClient(_build_llamaserver_app(), headers=AUTH_HEADERS)
        client.post(
            "/v1/nodes/register",
            json=_node_payload(
                "mini-1",
                router.base_url,
                swap_mode="router",
                allowed_models=["hy-mt2"],
            ),
        )
        # No /health/ready beforehand: request reaches the router directly.

        response = client.post(
            "/v1/chat/completions",
            json={"model": "hy-mt2", "messages": [{"role": "user", "content": "hi"}]},
        )

        assert response.status_code == 503
        assert response.json()["error"]["code"] == "model_loading"
        assert int(response.headers["Retry-After"]) > 0
        node = client.get("/v1/nodes").json()["nodes"][0]
        assert node["health"]["recent_failures"] == 0


def test_streaming_backend_loading_response_before_first_probe():
    with _FakeRouterServer(models={"hy-mt2": {"value": "loading"}}) as router:
        client = TestClient(_build_llamaserver_app(), headers=AUTH_HEADERS)
        client.post(
            "/v1/nodes/register",
            json=_node_payload(
                "mini-1", router.base_url, swap_mode="router", allowed_models=["hy-mt2"]
            ),
        )
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "hy-mt2",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "model_loading"
        assert int(response.headers["Retry-After"]) > 0
        assert client.get("/v1/nodes").json()["nodes"][0]["health"]["recent_failures"] == 0


def test_request_routes_to_ready_node_when_peer_router_node_is_loading():
    with (
        _FakeRouterServer(models={"hy-mt2": {"value": "loading"}}) as loading_router,
        _FakeRouterServer(
            models={"hy-mt2": {"value": "loaded"}},
            slots_by_model={"hy-mt2": [{"id": 0, "is_processing": False}]},
        ) as ready_router,
    ):
        client = TestClient(_build_llamaserver_app(), headers=AUTH_HEADERS)
        client.post(
            "/v1/nodes/register",
            json=_node_payload(
                "mini-loading",
                loading_router.base_url,
                swap_mode="router",
                allowed_models=["hy-mt2"],
            ),
        )
        client.post(
            "/v1/nodes/register",
            json=_node_payload(
                "mini-ready",
                ready_router.base_url,
                swap_mode="router",
                allowed_models=["hy-mt2"],
            ),
        )
        client.get("/health/ready")

        response = client.post(
            "/v1/chat/completions",
            json={"model": "hy-mt2", "messages": [{"role": "user", "content": "hi"}]},
        )

        assert response.status_code == 200
        assert response.headers["X-Backend-Id"] == "mini-ready"


async def test_router_backend_list_models_returns_only_servable_models():
    with _FakeRouterServer(
        models={
            "loaded-model": {"value": "loaded"},
            "sleeping-model": {"value": "sleeping"},
            "loading-model": {"value": "loading"},
            "unloaded-model": {"value": "unloaded"},
            "broken-model": {"value": "unloaded", "failed": True, "exit_code": 1},
        }
    ) as router:
        backend = LlamaCPPServerBackend(base_url=router.base_url, timeout=5.0, swap_mode="router")

        models = await backend.list_models()

        assert models == ["loaded-model", "sleeping-model"]
        assert ("GET", "/models") in router.requests
        assert ("GET", "/v1/models") not in router.requests


def test_non_router_node_behavior_is_unchanged():
    with _FakeSingleModelServer(model_id="gemma-4-e4b", fail_completions=True) as server:
        client = TestClient(_build_llamaserver_app(), headers=AUTH_HEADERS)
        client.post(
            "/v1/nodes/register",
            json=_node_payload(
                "cachy-1",
                server.base_url,
                allowed_models=["gemma-4-e4b"],
            ),
        )
        client.get("/health/ready")

        for _ in range(3):
            client.post(
                "/v1/chat/completions",
                json={"model": "gemma-4-e4b", "messages": [{"role": "user", "content": "hi"}]},
            )

        node = client.get("/v1/nodes").json()["nodes"][0]
        assert node["swap_mode"] == "none"
        assert node["models"] is None
        assert node["external_load"]["available"] is True
        assert node["external_load"]["total_slots"] == 2
        assert node["status"] == "offline"
        assert ("GET", "/slots") in server.requests
        assert not any(path == "/models" for _, path in server.requests)


def test_legacy_sqlite_row_without_swap_mode_defaults_to_none():
    db_path = new_registry_db_path()
    client = TestClient(_build_llamaserver_app(db_path=db_path), headers=AUTH_HEADERS)
    client.post(
        "/v1/nodes/register",
        json=_node_payload("legacy-node", "http://127.0.0.1:9999"),
    )
    with sqlite3.connect(db_path) as conn:
        (raw,) = conn.execute("SELECT capabilities FROM hosts").fetchone()
        legacy = {k: v for k, v in json.loads(raw).items() if k != "swap_mode"}
        conn.execute("UPDATE hosts SET capabilities = ?", (json.dumps(legacy),))

    restarted = TestClient(_build_llamaserver_app(db_path=db_path), headers=AUTH_HEADERS)

    node = restarted.get("/v1/nodes").json()["nodes"][0]
    assert node["swap_mode"] == "none"
    assert node["models"] is None


def test_invalid_swap_mode_is_rejected_with_400():
    client = TestClient(_build_llamaserver_app(), headers=AUTH_HEADERS)

    response = client.post(
        "/v1/nodes/register",
        json=_node_payload("mini-1", "http://127.0.0.1:9999", swap_mode="invalid"),
    )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


def test_patching_swap_mode_switches_node_between_router_and_none():
    client = TestClient(_build_llamaserver_app(), headers=AUTH_HEADERS)
    client.post(
        "/v1/nodes/register",
        json=_node_payload("mini-1", "http://127.0.0.1:9999"),
    )

    to_router = client.patch("/v1/nodes/mini-1", json={"swap_mode": "router"})

    assert to_router.status_code == 200
    assert client.get("/v1/nodes").json()["nodes"][0]["swap_mode"] == "router"
    assert client.get("/v1/nodes").json()["nodes"][0]["models"] == []

    to_none = client.patch("/v1/nodes/mini-1", json={"swap_mode": "none"})

    assert to_none.status_code == 200
    assert client.get("/v1/nodes").json()["nodes"][0]["swap_mode"] == "none"
    assert client.get("/v1/nodes").json()["nodes"][0]["models"] is None


def test_half_open_trial_connection_error_reopens_model_cooldown():
    class _FailingConnectionBackend:
        backend_id = "mini-1"

        async def check_health(self) -> BackendHealth:
            return BackendHealth(healthy=True, detail="ok")

        async def list_model_states(self, *, raise_on_timeout: bool = False):
            return [RouterModelState(id="broken", state="loaded")]

        async def complete(self, request) -> BackendResponse:
            raise BackendConnectionError("connection refused")

        async def stream(self, request):
            raise BackendConnectionError("connection refused")
            yield BackendChunk(content="", finish_reason="stop", usage=None)  # pragma: no cover

    health_monitor = HealthMonitor(
        failure_threshold=3, recovery_threshold=2, cooldown=timedelta(seconds=30)
    )
    policy = RoutingPolicy(rules=[PolicyRule(name="flat", score_fn=lambda c, ctx: 0.0)])
    app = create_app(
        registry=HostRegistry(new_registry_db_path()),
        router=RoutingEngine(policy),
        health_monitor=health_monitor,
        scheduling_queue=SchedulingQueue(),
        backend_factories={"fake": lambda caps: _FailingConnectionBackend()},
        metrics_registry=MetricsRegistry(),
        alert_evaluator=AlertEvaluator([]),
        key_store=_permissive_key_store(),
        external_load_probe=inert_external_load_probe(),
        dispatch_wait_timeout=0.1,
    )
    client = TestClient(app, headers=AUTH_HEADERS)
    client.post(
        "/v1/nodes/register",
        json={
            "host_id": "mini-1",
            "backend_type": "fake",
            "context_window": 8192,
            "base_url": "http://mini-1.local:8081",
            "max_concurrent_requests": 4,
            "allowed_models": ["broken"],
            "swap_mode": "router",
        },
    )
    client.get("/health/ready")

    opened_at = datetime.now(UTC)
    for _ in range(3):
        health_monitor.record_model_probe(
            "mini-1", "broken", healthy=False, at=opened_at
        )
    health_monitor._model_states[("mini-1", "broken")].unhealthy_since = (
        opened_at - timedelta(seconds=30)
    )

    non_stream = client.post(
        "/v1/chat/completions",
        json={"model": "broken", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert non_stream.status_code == 503
    now = datetime.now(UTC)
    assert health_monitor.has_model_trial("mini-1", "broken") is False
    assert health_monitor.is_model_healthy("mini-1", "broken", now) is False
    assert health_monitor.may_admit_model("mini-1", "broken", now) is False
    assert (
        health_monitor.may_admit_model("mini-1", "broken", now + timedelta(seconds=29))
        is False
    )
    assert client.get("/v1/nodes").json()["nodes"][0]["status"] == "online"

    health_monitor._model_states[("mini-1", "broken")].unhealthy_since = (
        datetime.now(UTC) - timedelta(seconds=30)
    )
    streamed = client.post(
        "/v1/chat/completions",
        json={
            "model": "broken",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )
    assert streamed.status_code == 503
    after_stream = datetime.now(UTC)
    assert health_monitor.has_model_trial("mini-1", "broken") is False
    assert health_monitor.may_admit_model("mini-1", "broken", after_stream) is False
    assert client.get("/v1/nodes").json()["nodes"][0]["status"] == "online"


def test_connection_failure_on_router_node_still_records_node_level_failure():
    class _FailingConnectionBackend:
        backend_id = "mini-1"

        async def check_health(self) -> BackendHealth:
            return BackendHealth(healthy=True, detail="ok")

        async def list_model_states(self, *, raise_on_timeout: bool = False):
            return [RouterModelState(id="hy-mt2", state="loaded")]

        async def complete(self, request) -> BackendResponse:
            raise BackendConnectionError("connection refused")

    policy = RoutingPolicy(rules=[PolicyRule(name="flat", score_fn=lambda c, ctx: 0.0)])
    app = create_app(
        registry=HostRegistry(new_registry_db_path()),
        router=RoutingEngine(policy),
        health_monitor=HealthMonitor(),
        scheduling_queue=SchedulingQueue(),
        backend_factories={"fake": lambda caps: _FailingConnectionBackend()},
        metrics_registry=MetricsRegistry(),
        alert_evaluator=AlertEvaluator([]),
        key_store=_permissive_key_store(),
        external_load_probe=inert_external_load_probe(),
        dispatch_wait_timeout=0.1,
    )
    client = TestClient(app, headers=AUTH_HEADERS)
    client.post(
        "/v1/nodes/register",
        json={
            "host_id": "mini-1",
            "backend_type": "fake",
            "context_window": 8192,
            "base_url": "http://mini-1.local:8081",
            "max_concurrent_requests": 4,
            "allowed_models": ["hy-mt2"],
            "swap_mode": "router",
        },
    )
    client.get("/health/ready")

    for _ in range(3):
        client.post(
            "/v1/chat/completions",
            json={"model": "hy-mt2", "messages": [{"role": "user", "content": "hi"}]},
        )

    node = client.get("/v1/nodes").json()["nodes"][0]
    assert node["status"] == "offline"
    assert node["health"]["recent_failures"] == 3


def test_streaming_failure_on_router_node_isolates_to_failing_model():
    class _StreamFailingBackend:
        backend_id = "mini-1"

        async def check_health(self) -> BackendHealth:
            return BackendHealth(healthy=True, detail="ok")

        async def list_model_states(self, *, raise_on_timeout: bool = False):
            return [
                RouterModelState(id="broken", state="unloaded"),
                RouterModelState(id="good", state="loaded"),
            ]

        async def stream(self, request):
            if request.model == "broken":
                raise BackendResponseError(500, "model failed to load")
            from llm_home_lab.backends.base import BackendChunk

            yield BackendChunk(
                content="ok",
                finish_reason="stop",
                usage={"prompt_tokens": 1, "completion_tokens": 1},
            )

    policy = RoutingPolicy(rules=[PolicyRule(name="flat", score_fn=lambda c, ctx: 0.0)])
    app = create_app(
        registry=HostRegistry(new_registry_db_path()),
        router=RoutingEngine(policy),
        health_monitor=HealthMonitor(),
        scheduling_queue=SchedulingQueue(),
        backend_factories={"fake": lambda caps: _StreamFailingBackend()},
        metrics_registry=MetricsRegistry(),
        alert_evaluator=AlertEvaluator([]),
        key_store=_permissive_key_store(),
        external_load_probe=inert_external_load_probe(),
        dispatch_wait_timeout=0.1,
    )
    client = TestClient(app, headers=AUTH_HEADERS, raise_server_exceptions=False)
    client.post(
        "/v1/nodes/register",
        json={
            "host_id": "mini-1",
            "backend_type": "fake",
            "context_window": 8192,
            "base_url": "http://mini-1.local:8081",
            "max_concurrent_requests": 4,
            "allowed_models": ["broken", "good"],
            "swap_mode": "router",
        },
    )
    client.get("/health/ready")

    for _ in range(3):
        client.post(
            "/v1/chat/completions",
            json={
                "model": "broken",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
        client.get("/health/ready")

    node = client.get("/v1/nodes").json()["nodes"][0]
    assert node["status"] == "online"
    assert client.get("/v1/capacity", params={"model": "broken"}).json() == {
        "total_max_concurrent_requests": 0
    }
    assert client.get("/v1/capacity", params={"model": "good"}).json() == {
        "total_max_concurrent_requests": 4
    }


def test_cancelled_half_open_complete_releases_model_trial():
    class _CancellingBackend:
        backend_id = "mini-1"

        async def check_health(self) -> BackendHealth:
            return BackendHealth(healthy=True, detail="ok")

        async def list_model_states(self, *, raise_on_timeout: bool = False):
            return [RouterModelState(id="broken", state="loaded")]

        async def complete(self, request) -> BackendResponse:
            if request.model == "broken" and getattr(self, "_fail_count", 0) < 3:
                self._fail_count = getattr(self, "_fail_count", 0) + 1
                raise BackendResponseError(500, "model failed")
            raise asyncio.CancelledError()

    health_monitor = HealthMonitor(
        failure_threshold=3, recovery_threshold=2, cooldown=timedelta(seconds=0)
    )
    policy = RoutingPolicy(rules=[PolicyRule(name="flat", score_fn=lambda c, ctx: 0.0)])
    app = create_app(
        registry=HostRegistry(new_registry_db_path()),
        router=RoutingEngine(policy),
        health_monitor=health_monitor,
        scheduling_queue=SchedulingQueue(),
        backend_factories={"fake": lambda caps: _CancellingBackend()},
        metrics_registry=MetricsRegistry(),
        alert_evaluator=AlertEvaluator([]),
        key_store=_permissive_key_store(),
        external_load_probe=inert_external_load_probe(),
        dispatch_wait_timeout=0.1,
    )
    client = TestClient(app, headers=AUTH_HEADERS, raise_server_exceptions=True)
    client.post(
        "/v1/nodes/register",
        json={
            "host_id": "mini-1",
            "backend_type": "fake",
            "context_window": 8192,
            "base_url": "http://mini-1.local:8081",
            "max_concurrent_requests": 4,
            "allowed_models": ["broken"],
            "swap_mode": "router",
        },
    )
    client.get("/health/ready")

    for _ in range(3):
        assert (
            client.post(
                "/v1/chat/completions",
                json={"model": "broken", "messages": [{"role": "user", "content": "hi"}]},
            ).status_code
            == 503
        )

    assert health_monitor.is_model_healthy("mini-1", "broken", datetime.now(UTC)) is False
    with pytest.raises((asyncio.CancelledError, RuntimeError)):
        client.post(
            "/v1/chat/completions",
            json={"model": "broken", "messages": [{"role": "user", "content": "hi"}]},
        )

    now = datetime.now(UTC)
    assert health_monitor.has_model_trial("mini-1", "broken") is False
    assert health_monitor.is_model_healthy("mini-1", "broken", now) is False
    assert health_monitor.may_admit_model("mini-1", "broken", now) is True
    assert client.get("/v1/capacity", params={"model": "broken"}).json() == {
        "total_max_concurrent_requests": 0
    }


async def test_stream_chunks_aclose_releases_outstanding_model_trial():
    class _HangingStreamBackend:
        async def stream(self, request):
            yield BackendChunk(
                content="partial",
                finish_reason=None,
                usage=None,
            )
            await asyncio.Event().wait()

    health_monitor = HealthMonitor(failure_threshold=1, cooldown=timedelta(seconds=30))
    health_monitor.record_model_probe("mini-1", "broken", healthy=False, at=datetime.now(UTC))
    at = datetime.now(UTC) + timedelta(seconds=30)
    assert health_monitor.claim_model_trial("mini-1", "broken", at) is True

    chunks = _stream_chunks(
        _HangingStreamBackend(),
        ChatCompletionRequest(model="broken", messages=[{"role": "user", "content": "hi"}]),
        MetricsRegistry(),
        CompletionLog(),
        health_monitor,
        "mini-1",
        router_mode=True,
    )
    assert await anext(chunks)
    await chunks.aclose()

    assert health_monitor.has_model_trial("mini-1", "broken") is False
    assert health_monitor.is_model_healthy("mini-1", "broken", at) is False
    assert health_monitor.may_admit_model("mini-1", "broken", at) is True

import asyncio
import json
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import httpx
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
from llm_home_lab.leases.book import NodeLeaseBook
from llm_home_lab.leases.models import LeaseSettings, ReservableHost
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


class _FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, *, seconds: float) -> datetime:
        self.now += timedelta(seconds=seconds)
        return self.now


class _FakeRouterServer:
    """Loopback HTTP server emulating llama-server in router mode (--models-max 1)."""

    def __init__(
        self,
        *,
        models: dict[str, dict[str, object]],
        slots_by_model: dict[str, list[dict[str, object]]] | None = None,
        broken_models: set[str] | None = None,
        mark_broken_failed: bool = True,
        health_ok: bool = True,
        completion_content: str = "ok",
        completion_finish_reason: str = "stop",
        completion_tokens: int = 1,
        on_completion: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self.models = {k: dict(v) for k, v in models.items()}
        self.slots_by_model = dict(slots_by_model or {})
        self.broken_models = set(broken_models or set())
        self.mark_broken_failed = mark_broken_failed
        self.health_ok = health_ok
        self.completion_content = completion_content
        self.completion_finish_reason = completion_finish_reason
        self.completion_tokens = completion_tokens
        self.on_completion = on_completion
        self.requests: list[tuple[str, str]] = []
        self.completion_payloads: list[dict[str, object]] = []
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
                    outer.completion_payloads.append(payload)
                    on_completion = outer.on_completion
                if on_completion is not None:
                    on_completion(payload)
                with outer._lock:
                    model = payload.get("model", "")
                    if model in outer.broken_models:
                        if outer.mark_broken_failed:
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
                    content = outer.completion_content
                    finish_reason = outer.completion_finish_reason
                    completion_tokens = outer.completion_tokens

                chunks = []
                if content:
                    content_chunk = {
                        "choices": [{"delta": {"content": content}, "finish_reason": None}]
                    }
                    chunks.append(f"data: {json.dumps(content_chunk)}\n\n")
                finish_chunk = {"choices": [{"delta": {}, "finish_reason": finish_reason}]}
                usage_chunk = {
                    "choices": [],
                    "usage": {"prompt_tokens": 3, "completion_tokens": completion_tokens},
                }
                chunks.append(f"data: {json.dumps(finish_chunk)}\n\n")
                chunks.append(f"data: {json.dumps(usage_chunk)}\n\n")
                chunks.append("data: [DONE]\n\n")
                sse = "".join(chunks).encode()
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
    health_clock: Callable[[], datetime] | None = None,
    lease_clock: Callable[[], datetime] | None = None,
    health_poll_interval: float | None = None,
    health_timeout: float = 10.0,
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
                health_timeout=health_timeout,
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
        lease_clock=lease_clock,
        health_clock=health_clock,
        health_poll_interval=health_poll_interval,
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
            health_monitor.may_admit_model("mini-1", "broken-missing", now + timedelta(seconds=29))
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
        health_monitor.record_model_probe("mini-1", "broken", healthy=False, at=opened_at)
    health_monitor._model_states[("mini-1", "broken")].unhealthy_since = opened_at - timedelta(
        seconds=30
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
    assert health_monitor.may_admit_model("mini-1", "broken", now + timedelta(seconds=29)) is False
    assert client.get("/v1/nodes").json()["nodes"][0]["status"] == "online"

    health_monitor._model_states[("mini-1", "broken")].unhealthy_since = datetime.now(
        UTC
    ) - timedelta(seconds=30)
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


def test_idle_unhealthy_loaded_model_recovers_after_cooldown_without_client_traffic():
    clock = _FakeClock()
    with _FakeRouterServer(
        models={"gemma-4-26B-A4B-it": {"value": "loaded"}},
        slots_by_model={"gemma-4-26B-A4B-it": [{"id": 0, "is_processing": False}]},
        broken_models={"gemma-4-26B-A4B-it"},
        mark_broken_failed=False,
        completion_finish_reason="length",
    ) as router:
        health_monitor = HealthMonitor(
            failure_threshold=3, recovery_threshold=2, cooldown=timedelta(seconds=30)
        )
        completion_log = CompletionLog(clock=clock)
        client = TestClient(
            _build_llamaserver_app(
                health_monitor=health_monitor,
                completion_log=completion_log,
                health_clock=clock,
                lease_clock=clock,
            ),
            headers=AUTH_HEADERS,
        )
        client.post(
            "/v1/nodes/register",
            json=_node_payload(
                "mini-2",
                router.base_url,
                swap_mode="router",
                max_concurrent_requests=6,
                allowed_models=["gemma-4-26B-A4B-it"],
            ),
        )
        client.get("/health/ready")

        for _ in range(3):
            assert (
                client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "gemma-4-26B-A4B-it",
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                ).status_code
                == 503
            )

        assert len(router.completion_payloads) == 3
        assert client.get("/v1/capacity", params={"model": "gemma-4-26B-A4B-it"}).json() == {
            "total_max_concurrent_requests": 0
        }
        node = client.get("/v1/nodes").json()["nodes"][0]
        assert node["status"] == "online"
        assert node["models"][0]["state"] == "loaded"
        assert node["models"][0]["healthy"] is False

        # Underlying model is healthy again, but no client traffic arrives.
        router.broken_models.clear()

        # Before cooldown elapses: probing must NOT send a completion trial.
        clock.advance(seconds=29)
        assert client.get("/health/ready").status_code == 200
        assert len(router.completion_payloads) == 3
        assert health_monitor.is_model_healthy("mini-2", "gemma-4-26B-A4B-it", clock()) is False

        # Once cooldown elapses: probe runs exactly one minimal 1-token trial and closes circuit.
        clock.advance(seconds=1)
        assert client.get("/health/ready").status_code == 200
        assert len(router.completion_payloads) == 4
        assert router.completion_payloads[-1]["model"] == "gemma-4-26B-A4B-it"
        assert router.completion_payloads[-1]["max_tokens"] == 1
        assert health_monitor.is_model_healthy("mini-2", "gemma-4-26B-A4B-it", clock()) is True
        assert client.get("/v1/capacity", params={"model": "gemma-4-26B-A4B-it"}).json() == {
            "total_max_concurrent_requests": 6
        }
        recovered_node = client.get("/v1/nodes").json()["nodes"][0]
        assert recovered_node["models"][0]["healthy"] is True
        assert recovered_node["throughput"]["tasks_per_hour"] == 0.0

        # Subsequent probes once healthy must not send extra completions.
        clock.advance(seconds=60)
        assert client.get("/health/ready").status_code == 200
        assert len(router.completion_payloads) == 4


def test_background_poller_recovers_idle_unhealthy_router_model_without_requests():
    clock = _FakeClock()
    probed = threading.Event()
    with _FakeRouterServer(
        models={"gemma-4-26B-A4B-it": {"value": "loaded"}},
        slots_by_model={"gemma-4-26B-A4B-it": [{"id": 0, "is_processing": False}]},
        completion_finish_reason="length",
        on_completion=lambda _payload: probed.set(),
    ) as router:
        health_monitor = HealthMonitor(failure_threshold=1, cooldown=timedelta(seconds=30))
        health_monitor.record_model_probe("mini-3", "gemma-4-26B-A4B-it", healthy=False, at=clock())
        clock.advance(seconds=30)

        app = _build_llamaserver_app(
            health_monitor=health_monitor,
            health_clock=clock,
            lease_clock=clock,
            health_poll_interval=0.01,
        )
        with TestClient(app, headers=AUTH_HEADERS) as client:
            client.post(
                "/v1/nodes/register",
                json=_node_payload(
                    "mini-3",
                    router.base_url,
                    swap_mode="router",
                    max_concurrent_requests=4,
                    allowed_models=["gemma-4-26B-A4B-it"],
                ),
            )
            assert probed.wait(timeout=5.0)
            for _ in range(100):
                if health_monitor.is_model_healthy("mini-3", "gemma-4-26B-A4B-it", clock()):
                    break
                threading.Event().wait(0.01)

            assert health_monitor.is_model_healthy("mini-3", "gemma-4-26B-A4B-it", clock()) is True
            assert len(router.completion_payloads) == 1
            assert router.completion_payloads[0]["max_tokens"] == 1
            assert client.get("/v1/capacity", params={"model": "gemma-4-26B-A4B-it"}).json() == {
                "total_max_concurrent_requests": 4
            }


def test_proactive_probe_accepts_reasoning_length_token_and_rejects_empty_stop():
    clock = _FakeClock()
    with _FakeRouterServer(
        models={"gemma-4-26B-A4B-it": {"value": "loaded"}},
        slots_by_model={"gemma-4-26B-A4B-it": [{"id": 0, "is_processing": False}]},
        completion_content="",
        completion_finish_reason="stop",
        completion_tokens=0,
    ) as router:
        health_monitor = HealthMonitor(failure_threshold=1, cooldown=timedelta(seconds=30))
        client = TestClient(
            _build_llamaserver_app(
                health_monitor=health_monitor,
                health_clock=clock,
                lease_clock=clock,
            ),
            headers=AUTH_HEADERS,
        )
        client.post(
            "/v1/nodes/register",
            json=_node_payload(
                "mini-2",
                router.base_url,
                swap_mode="router",
                allowed_models=["gemma-4-26B-A4B-it"],
            ),
        )
        health_monitor.record_model_probe("mini-2", "gemma-4-26B-A4B-it", healthy=False, at=clock())

        # Empty content with finish_reason="stop" is degenerate; stays unhealthy, restarts cooldown.
        clock.advance(seconds=30)
        client.get("/health/ready")
        assert len(router.completion_payloads) == 1
        assert health_monitor.is_model_healthy("mini-2", "gemma-4-26B-A4B-it", clock()) is False

        # Reasoning model emitting 1 token hits max_tokens=1 -> length + 1 token -> recovers.
        router.completion_finish_reason = "length"
        router.completion_tokens = 1
        clock.advance(seconds=30)
        client.get("/health/ready")
        assert len(router.completion_payloads) == 2
        assert health_monitor.is_model_healthy("mini-2", "gemma-4-26B-A4B-it", clock()) is True


def test_proactive_probe_skips_unloaded_loading_sleeping_failed_and_not_allowed_models():
    clock = _FakeClock()
    with _FakeRouterServer(
        models={
            "unloaded-m": {"value": "unloaded"},
            "loading-m": {"value": "loading"},
            "sleeping-m": {"value": "sleeping"},
            "failed-m": {"value": "loaded", "failed": True, "exit_code": 1},
            "disallowed-loaded": {"value": "loaded"},
        },
        slots_by_model={"disallowed-loaded": [{"id": 0, "is_processing": False}]},
    ) as router:
        health_monitor = HealthMonitor(failure_threshold=1, cooldown=timedelta(seconds=30))
        client = TestClient(
            _build_llamaserver_app(
                health_monitor=health_monitor,
                health_clock=clock,
                lease_clock=clock,
            ),
            headers=AUTH_HEADERS,
        )
        client.post(
            "/v1/nodes/register",
            json=_node_payload(
                "mini-1",
                router.base_url,
                swap_mode="router",
                allowed_models=["unloaded-m", "loading-m", "sleeping-m", "failed-m"],
            ),
        )
        for model in ("unloaded-m", "loading-m", "sleeping-m", "failed-m", "disallowed-loaded"):
            health_monitor.record_model_probe("mini-1", model, healthy=False, at=clock())

        clock.advance(seconds=60)
        assert client.get("/health/ready").status_code == 200

        assert router.completion_payloads == []
        for model in ("unloaded-m", "loading-m", "sleeping-m", "failed-m", "disallowed-loaded"):
            assert health_monitor.is_model_healthy("mini-1", model, clock()) is False


def test_proactive_probe_never_probes_leased_node_and_recovers_after_release():
    clock = _FakeClock()
    with _FakeRouterServer(
        models={"gemma-4-26B-A4B-it": {"value": "loaded"}},
        slots_by_model={"gemma-4-26B-A4B-it": [{"id": 0, "is_processing": False}]},
    ) as router:
        health_monitor = HealthMonitor(failure_threshold=1, cooldown=timedelta(seconds=30))
        client = TestClient(
            _build_llamaserver_app(
                health_monitor=health_monitor,
                health_clock=clock,
                lease_clock=clock,
            ),
            headers=AUTH_HEADERS,
        )
        client.post(
            "/v1/nodes/register",
            json=_node_payload(
                "mini-1",
                router.base_url,
                swap_mode="router",
                allowed_models=["gemma-4-26B-A4B-it"],
            ),
        )
        client.get("/health/ready")
        lease_res = client.post(
            "/v1/node-leases",
            json={"holder": "real-llm-check", "host_id": "mini-1"},
        )
        assert lease_res.status_code == 201
        token = lease_res.json()["token"]

        health_monitor.record_model_probe("mini-1", "gemma-4-26B-A4B-it", healthy=False, at=clock())
        clock.advance(seconds=30)

        assert client.get("/health/ready").status_code == 200
        assert router.completion_payloads == []
        assert health_monitor.is_model_healthy("mini-1", "gemma-4-26B-A4B-it", clock()) is False

        release = client.delete("/v1/node-leases", headers={"X-LLM-Node-Lease": token})
        assert release.status_code == 200
        assert client.get("/health/ready").status_code == 200
        assert len(router.completion_payloads) == 1
        assert health_monitor.is_model_healthy("mini-1", "gemma-4-26B-A4B-it", clock()) is True


def test_proactive_probe_skips_when_node_health_check_fails():
    clock = _FakeClock()
    with _FakeRouterServer(
        models={"gemma-4-26B-A4B-it": {"value": "loaded"}},
        slots_by_model={"gemma-4-26B-A4B-it": [{"id": 0, "is_processing": False}]},
        health_ok=False,
    ) as router:
        health_monitor = HealthMonitor(failure_threshold=1, cooldown=timedelta(seconds=30))
        client = TestClient(
            _build_llamaserver_app(
                health_monitor=health_monitor,
                health_clock=clock,
                lease_clock=clock,
            ),
            headers=AUTH_HEADERS,
        )
        client.post(
            "/v1/nodes/register",
            json=_node_payload(
                "mini-1",
                router.base_url,
                swap_mode="router",
                allowed_models=["gemma-4-26B-A4B-it"],
            ),
        )
        health_monitor.record_model_probe("mini-1", "gemma-4-26B-A4B-it", healthy=False, at=clock())
        clock.advance(seconds=30)

        assert client.get("/health/ready").status_code == 503
        assert router.completion_payloads == []


def test_proactive_probe_failure_restarts_cooldown_and_limits_to_one_probe_per_cooldown():
    clock = _FakeClock()
    with _FakeRouterServer(
        models={"gemma-4-26B-A4B-it": {"value": "loaded"}},
        slots_by_model={"gemma-4-26B-A4B-it": [{"id": 0, "is_processing": False}]},
        broken_models={"gemma-4-26B-A4B-it"},
        mark_broken_failed=False,
    ) as router:
        health_monitor = HealthMonitor(failure_threshold=1, cooldown=timedelta(seconds=30))
        client = TestClient(
            _build_llamaserver_app(
                health_monitor=health_monitor,
                health_clock=clock,
                lease_clock=clock,
            ),
            headers=AUTH_HEADERS,
        )
        client.post(
            "/v1/nodes/register",
            json=_node_payload(
                "mini-1",
                router.base_url,
                swap_mode="router",
                allowed_models=["gemma-4-26B-A4B-it"],
            ),
        )
        client.get("/health/ready")
        health_monitor.record_model_probe("mini-1", "gemma-4-26B-A4B-it", healthy=False, at=clock())

        # First trial after 30s fails -> keeps pair unhealthy, restarts cooldown, node stays online.
        clock.advance(seconds=30)
        assert client.get("/health/ready").status_code == 200
        assert len(router.completion_payloads) == 1
        assert health_monitor.is_model_healthy("mini-1", "gemma-4-26B-A4B-it", clock()) is False
        assert health_monitor.has_model_trial("mini-1", "gemma-4-26B-A4B-it") is False
        assert client.get("/v1/capacity", params={"model": "gemma-4-26B-A4B-it"}).json() == {
            "total_max_concurrent_requests": 0
        }

        # Multiple probes during the restarted cooldown must not send another completion.
        clock.advance(seconds=15)
        client.get("/health/ready")
        clock.advance(seconds=14)
        client.get("/health/ready")
        assert len(router.completion_payloads) == 1

        # After the second full 30s cooldown, exactly one more probe runs.
        clock.advance(seconds=1)
        client.get("/health/ready")
        assert len(router.completion_payloads) == 2
        assert health_monitor.is_model_healthy("mini-1", "gemma-4-26B-A4B-it", clock()) is False


def test_proactive_probe_loading_503_releases_trial_without_restarting_cooldown():
    clock = _FakeClock()

    class _LoadingOnCompleteBackend:
        backend_id = "mini-1"
        health_timeout = 5.0

        def __init__(self) -> None:
            self.calls = 0
            self.return_loading = True

        async def check_health(self) -> BackendHealth:
            return BackendHealth(healthy=True, detail="ok")

        async def list_model_states(self, *, raise_on_timeout: bool = False):
            return [RouterModelState(id="gemma", state="loaded")]

        async def complete(self, request: ChatCompletionRequest) -> BackendResponse:
            self.calls += 1
            if self.return_loading:
                raise BackendResponseError(503, '{"error":{"message":"Loading model"}}')
            return BackendResponse(
                model=request.model,
                content="ok",
                finish_reason="length",
                prompt_tokens=2,
                completion_tokens=1,
            )

    backend = _LoadingOnCompleteBackend()
    health_monitor = HealthMonitor(failure_threshold=1, cooldown=timedelta(seconds=30))
    policy = RoutingPolicy(rules=[PolicyRule(name="flat", score_fn=lambda c, ctx: 0.0)])
    app = create_app(
        registry=HostRegistry(new_registry_db_path()),
        router=RoutingEngine(policy),
        health_monitor=health_monitor,
        scheduling_queue=SchedulingQueue(),
        backend_factories={"fake": lambda caps: backend},
        metrics_registry=MetricsRegistry(),
        alert_evaluator=AlertEvaluator([]),
        key_store=_permissive_key_store(),
        external_load_probe=inert_external_load_probe(),
        health_clock=clock,
        lease_clock=clock,
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
            "allowed_models": ["gemma"],
            "swap_mode": "router",
        },
    )
    health_monitor.record_model_probe("mini-1", "gemma", healthy=False, at=clock())
    clock.advance(seconds=30)

    client.get("/health/ready")
    assert backend.calls == 1
    assert health_monitor.has_model_trial("mini-1", "gemma") is False
    assert health_monitor.is_model_healthy("mini-1", "gemma", clock()) is False
    # Cooldown was NOT restarted, so the next tick recovers immediately once loaded.
    assert health_monitor.may_admit_model("mini-1", "gemma", clock()) is True

    backend.return_loading = False
    client.get("/health/ready")
    assert backend.calls == 2
    assert health_monitor.is_model_healthy("mini-1", "gemma", clock()) is True


async def test_proactive_probe_and_real_request_never_double_trial():
    clock = _FakeClock()

    class _GatedBackend:
        backend_id = "mini-1"
        health_timeout = 5.0

        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.active_calls = 0
            self.max_concurrent_calls = 0
            self.seen_requests: list[ChatCompletionRequest] = []

        async def check_health(self) -> BackendHealth:
            return BackendHealth(healthy=True, detail="ok")

        async def list_model_states(self, *, raise_on_timeout: bool = False):
            return [RouterModelState(id="gemma", state="loaded")]

        async def complete(self, request: ChatCompletionRequest) -> BackendResponse:
            self.seen_requests.append(request)
            self.active_calls += 1
            self.max_concurrent_calls = max(self.max_concurrent_calls, self.active_calls)
            try:
                if len(self.seen_requests) == 1:
                    self.started.set()
                    await self.release.wait()
                return BackendResponse(
                    model=request.model,
                    content="ok",
                    finish_reason="stop",
                    prompt_tokens=3,
                    completion_tokens=1,
                )
            finally:
                self.active_calls -= 1

    # Direction 1: proactive probe claims trial first; incoming real request must not double-trial.
    backend = _GatedBackend()
    health_monitor = HealthMonitor(failure_threshold=1, cooldown=timedelta(seconds=30))
    policy = RoutingPolicy(rules=[PolicyRule(name="flat", score_fn=lambda c, ctx: 0.0)])
    app = create_app(
        registry=HostRegistry(new_registry_db_path()),
        router=RoutingEngine(policy),
        health_monitor=health_monitor,
        scheduling_queue=SchedulingQueue(),
        backend_factories={"fake": lambda caps: backend},
        metrics_registry=MetricsRegistry(),
        alert_evaluator=AlertEvaluator([]),
        key_store=_permissive_key_store(),
        external_load_probe=inert_external_load_probe(),
        health_clock=clock,
        lease_clock=clock,
        dispatch_wait_timeout=1.0,
        dispatch_poll_interval=0.01,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers=AUTH_HEADERS
    ) as client:
        await client.post(
            "/v1/nodes/register",
            json={
                "host_id": "mini-1",
                "backend_type": "fake",
                "context_window": 8192,
                "base_url": "http://mini-1.local:8081",
                "max_concurrent_requests": 4,
                "allowed_models": ["gemma"],
                "swap_mode": "router",
            },
        )
        health_monitor.record_model_probe("mini-1", "gemma", healthy=False, at=clock())
        clock.advance(seconds=30)

        probe_task = asyncio.create_task(client.get("/health/ready"))
        await asyncio.wait_for(backend.started.wait(), timeout=2.0)
        assert health_monitor.has_model_trial("mini-1", "gemma") is True

        real_task = asyncio.create_task(
            client.post(
                "/v1/chat/completions",
                json={"model": "gemma", "messages": [{"role": "user", "content": "real"}]},
            )
        )
        # Let the real request run through admission and polling while the probe is still in flight.
        await asyncio.sleep(0.05)
        assert len(backend.seen_requests) == 1
        assert backend.seen_requests[0].max_tokens == 1
        assert backend.max_concurrent_calls == 1

        # Releasing the probe closes the circuit; the queued real request then proceeds normally.
        backend.release.set()
        probe_res, real_res = await asyncio.gather(probe_task, real_task)
        assert probe_res.status_code == 200
        assert real_res.status_code == 200
        assert len(backend.seen_requests) == 2
        assert backend.seen_requests[1].messages[0].content == "real"
        assert backend.max_concurrent_calls == 1

    # Direction 2: real request claims trial first; periodic probe must NOT send a second trial.
    backend2 = _GatedBackend()
    health_monitor2 = HealthMonitor(failure_threshold=1, cooldown=timedelta(seconds=30))
    app2 = create_app(
        registry=HostRegistry(new_registry_db_path()),
        router=RoutingEngine(policy),
        health_monitor=health_monitor2,
        scheduling_queue=SchedulingQueue(),
        backend_factories={"fake": lambda caps: backend2},
        metrics_registry=MetricsRegistry(),
        alert_evaluator=AlertEvaluator([]),
        key_store=_permissive_key_store(),
        external_load_probe=inert_external_load_probe(),
        health_clock=clock,
        lease_clock=clock,
        dispatch_wait_timeout=1.0,
        dispatch_poll_interval=0.01,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app2), base_url="http://test", headers=AUTH_HEADERS
    ) as client2:
        await client2.post(
            "/v1/nodes/register",
            json={
                "host_id": "mini-1",
                "backend_type": "fake",
                "context_window": 8192,
                "base_url": "http://mini-1.local:8081",
                "max_concurrent_requests": 4,
                "allowed_models": ["gemma"],
                "swap_mode": "router",
            },
        )
        health_monitor2.record_model_probe("mini-1", "gemma", healthy=False, at=clock())
        clock.advance(seconds=30)

        real_task2 = asyncio.create_task(
            client2.post(
                "/v1/chat/completions",
                json={"model": "gemma", "messages": [{"role": "user", "content": "real-trial"}]},
            )
        )
        await asyncio.wait_for(backend2.started.wait(), timeout=2.0)
        assert health_monitor2.has_model_trial("mini-1", "gemma") is True

        # Probe runs while real request trial is in flight -> must NOT issue a second trial.
        probe_res2 = await client2.get("/health/ready")
        assert probe_res2.status_code == 200
        assert len(backend2.seen_requests) == 1
        assert backend2.seen_requests[0].messages[0].content == "real-trial"

        backend2.release.set()
        real_res2 = await real_task2
        assert real_res2.status_code == 200
        assert len(backend2.seen_requests) == 1
        assert health_monitor2.is_model_healthy("mini-1", "gemma", clock()) is True


def test_non_router_node_recovery_remains_unchanged_without_completion_probes():
    clock = _FakeClock()
    with _FakeSingleModelServer(model_id="gemma-4-e4b", fail_completions=True) as server:
        health_monitor = HealthMonitor(
            failure_threshold=3, recovery_threshold=2, cooldown=timedelta(seconds=30)
        )
        client = TestClient(
            _build_llamaserver_app(
                health_monitor=health_monitor,
                health_clock=clock,
                lease_clock=clock,
            ),
            headers=AUTH_HEADERS,
        )
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

        assert client.get("/v1/nodes").json()["nodes"][0]["status"] == "offline"
        posts_before = sum(1 for method, _ in server.requests if method == "POST")
        assert posts_before == 3

        clock.advance(seconds=30)
        client.get("/health/ready")
        client.get("/health/ready")

        assert client.get("/v1/nodes").json()["nodes"][0]["status"] == "online"
        posts_after = sum(1 for method, _ in server.requests if method == "POST")
        assert posts_after == posts_before


def test_proactive_probe_timeout_keeps_model_unhealthy_and_restarts_cooldown():
    clock = _FakeClock()

    class _HangingProbeBackend:
        backend_id = "mini-1"
        health_timeout = 0.05

        def __init__(self) -> None:
            self.calls = 0

        async def check_health(self) -> BackendHealth:
            return BackendHealth(healthy=True, detail="ok")

        async def list_model_states(self, *, raise_on_timeout: bool = False):
            return [RouterModelState(id="gemma", state="loaded")]

        async def complete(self, request: ChatCompletionRequest) -> BackendResponse:
            self.calls += 1
            await asyncio.Event().wait()
            raise AssertionError("unreachable")  # pragma: no cover

    backend = _HangingProbeBackend()
    registry = HostRegistry(new_registry_db_path())
    health_monitor = HealthMonitor(failure_threshold=1, cooldown=timedelta(seconds=30))
    policy = RoutingPolicy(rules=[PolicyRule(name="flat", score_fn=lambda c, ctx: 0.0)])
    app = create_app(
        registry=registry,
        router=RoutingEngine(policy),
        health_monitor=health_monitor,
        scheduling_queue=SchedulingQueue(),
        backend_factories={"fake": lambda caps: backend},
        metrics_registry=MetricsRegistry(),
        alert_evaluator=AlertEvaluator([]),
        key_store=_permissive_key_store(),
        external_load_probe=inert_external_load_probe(),
        health_clock=clock,
        lease_clock=clock,
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
            "allowed_models": ["gemma"],
            "swap_mode": "router",
        },
    )
    health_monitor.record_model_probe("mini-1", "gemma", healthy=False, at=clock())
    clock.advance(seconds=30)

    assert client.get("/health/ready").status_code == 200
    assert backend.calls == 1
    assert registry.in_flight("mini-1") == 0
    assert health_monitor.has_model_trial("mini-1", "gemma") is False
    assert health_monitor.is_model_healthy("mini-1", "gemma", clock()) is False
    assert (
        health_monitor.may_admit_model("mini-1", "gemma", clock() + timedelta(seconds=29)) is False
    )


def test_proactive_probe_releases_trial_when_acquire_slot_raises():
    clock = _FakeClock()

    class _DeregisterOnAcquire(HostRegistry):
        def acquire_slot(self, host_id: str) -> None:
            self.deregister(host_id)
            super().acquire_slot(host_id)

    class _HealthyListBackend:
        backend_id = "mini-1"
        health_timeout = 5.0

        async def check_health(self) -> BackendHealth:
            return BackendHealth(healthy=True, detail="ok")

        async def list_model_states(self, *, raise_on_timeout: bool = False):
            return [RouterModelState(id="gemma", state="loaded")]

        async def complete(self, request: ChatCompletionRequest) -> BackendResponse:
            raise AssertionError("probe must not run after acquire failure")

    backend = _HealthyListBackend()
    health_monitor = HealthMonitor(failure_threshold=1, cooldown=timedelta(seconds=30))
    policy = RoutingPolicy(rules=[PolicyRule(name="flat", score_fn=lambda c, ctx: 0.0)])
    app = create_app(
        registry=_DeregisterOnAcquire(new_registry_db_path()),
        router=RoutingEngine(policy),
        health_monitor=health_monitor,
        scheduling_queue=SchedulingQueue(),
        backend_factories={"fake": lambda caps: backend},
        metrics_registry=MetricsRegistry(),
        alert_evaluator=AlertEvaluator([]),
        key_store=_permissive_key_store(),
        external_load_probe=inert_external_load_probe(),
        health_clock=clock,
        lease_clock=clock,
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
            "allowed_models": ["gemma"],
            "swap_mode": "router",
        },
    )
    health_monitor.record_model_probe("mini-1", "gemma", healthy=False, at=clock())
    clock.advance(seconds=30)

    assert client.get("/health/ready").status_code == 200
    assert health_monitor.has_model_trial("mini-1", "gemma") is False
    assert health_monitor.may_admit_model("mini-1", "gemma", clock()) is True


def test_proactive_probe_unexpected_complete_error_restarts_cooldown_without_failing_ready():
    clock = _FakeClock()

    class _MalformedCompleteBackend:
        backend_id = "mini-1"
        health_timeout = 5.0

        def __init__(self) -> None:
            self.calls = 0

        async def check_health(self) -> BackendHealth:
            return BackendHealth(healthy=True, detail="ok")

        async def list_model_states(self, *, raise_on_timeout: bool = False):
            return [RouterModelState(id="gemma", state="loaded")]

        async def complete(self, request: ChatCompletionRequest) -> BackendResponse:
            self.calls += 1
            raise json.JSONDecodeError("Expecting value", "", 0)

    backend = _MalformedCompleteBackend()
    registry = HostRegistry(new_registry_db_path())
    health_monitor = HealthMonitor(failure_threshold=1, cooldown=timedelta(seconds=30))
    policy = RoutingPolicy(rules=[PolicyRule(name="flat", score_fn=lambda c, ctx: 0.0)])
    app = create_app(
        registry=registry,
        router=RoutingEngine(policy),
        health_monitor=health_monitor,
        scheduling_queue=SchedulingQueue(),
        backend_factories={"fake": lambda caps: backend},
        metrics_registry=MetricsRegistry(),
        alert_evaluator=AlertEvaluator([]),
        key_store=_permissive_key_store(),
        external_load_probe=inert_external_load_probe(),
        health_clock=clock,
        lease_clock=clock,
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
            "allowed_models": ["gemma"],
            "swap_mode": "router",
        },
    )
    health_monitor.record_model_probe("mini-1", "gemma", healthy=False, at=clock())
    clock.advance(seconds=30)

    assert client.get("/health/ready").status_code == 200
    assert backend.calls == 1
    assert registry.in_flight("mini-1") == 0
    assert health_monitor.has_model_trial("mini-1", "gemma") is False
    assert health_monitor.is_model_healthy("mini-1", "gemma", clock()) is False
    assert health_monitor.may_admit_model("mini-1", "gemma", clock()) is False
    assert (
        health_monitor.may_admit_model("mini-1", "gemma", clock() + timedelta(seconds=29)) is False
    )

    client.get("/health/ready")
    assert backend.calls == 1

    clock.advance(seconds=30)
    assert client.get("/health/ready").status_code == 200
    assert backend.calls == 2


def test_proactive_probe_releases_slot_for_registration_acquired():
    clock = _FakeClock()

    class _SwapRegistrationOnAcquire(HostRegistry):
        def acquire_slot(self, host_id: str) -> None:
            host = self.get(host_id)
            self.deregister(host_id)
            self.register(host_id, host.capabilities, host.capacity, at=host.last_seen)
            super().acquire_slot(host_id)

    class _HealthyListBackend:
        backend_id = "mini-1"
        health_timeout = 5.0

        async def check_health(self) -> BackendHealth:
            return BackendHealth(healthy=True, detail="ok")

        async def list_model_states(self, *, raise_on_timeout: bool = False):
            return [RouterModelState(id="gemma", state="loaded")]

        async def complete(self, request: ChatCompletionRequest) -> BackendResponse:
            raise AssertionError("probe must not run after registration swap")

    backend = _HealthyListBackend()
    registry = _SwapRegistrationOnAcquire(new_registry_db_path())
    health_monitor = HealthMonitor(failure_threshold=1, cooldown=timedelta(seconds=30))
    policy = RoutingPolicy(rules=[PolicyRule(name="flat", score_fn=lambda c, ctx: 0.0)])
    app = create_app(
        registry=registry,
        router=RoutingEngine(policy),
        health_monitor=health_monitor,
        scheduling_queue=SchedulingQueue(),
        backend_factories={"fake": lambda caps: backend},
        metrics_registry=MetricsRegistry(),
        alert_evaluator=AlertEvaluator([]),
        key_store=_permissive_key_store(),
        external_load_probe=inert_external_load_probe(),
        health_clock=clock,
        lease_clock=clock,
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
            "allowed_models": ["gemma"],
            "swap_mode": "router",
        },
    )
    original = registry.get("mini-1").registration_id
    health_monitor.record_model_probe("mini-1", "gemma", healthy=False, at=clock())
    clock.advance(seconds=30)

    assert client.get("/health/ready").status_code == 200
    assert registry.get("mini-1").registration_id is not original
    assert registry.in_flight("mini-1") == 0
    assert health_monitor.has_model_trial("mini-1", "gemma") is False
    assert health_monitor.may_admit_model("mini-1", "gemma", clock()) is True


def test_proactive_probe_soft_reregister_on_acquire_releases_without_probing():
    clock = _FakeClock()

    class _SoftSwapOnAcquire(HostRegistry):
        def acquire_slot(self, host_id: str) -> None:
            host = self.get(host_id)
            self.register(
                host_id,
                replace(host.capabilities, base_url="http://mini-1.local:9090"),
                host.capacity,
                at=host.last_seen,
            )
            super().acquire_slot(host_id)

    class _HealthyListBackend:
        backend_id = "mini-1"
        health_timeout = 5.0

        async def check_health(self) -> BackendHealth:
            return BackendHealth(healthy=True, detail="ok")

        async def list_model_states(self, *, raise_on_timeout: bool = False):
            return [RouterModelState(id="gemma", state="loaded")]

        async def complete(self, request: ChatCompletionRequest) -> BackendResponse:
            raise AssertionError("probe must not run after soft re-register on acquire")

    backend = _HealthyListBackend()
    registry = _SoftSwapOnAcquire(new_registry_db_path())
    health_monitor = HealthMonitor(failure_threshold=1, cooldown=timedelta(seconds=30))
    policy = RoutingPolicy(rules=[PolicyRule(name="flat", score_fn=lambda c, ctx: 0.0)])
    app = create_app(
        registry=registry,
        router=RoutingEngine(policy),
        health_monitor=health_monitor,
        scheduling_queue=SchedulingQueue(),
        backend_factories={"fake": lambda caps: backend},
        metrics_registry=MetricsRegistry(),
        alert_evaluator=AlertEvaluator([]),
        key_store=_permissive_key_store(),
        external_load_probe=inert_external_load_probe(),
        health_clock=clock,
        lease_clock=clock,
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
            "allowed_models": ["gemma"],
            "swap_mode": "router",
        },
    )
    original_host = registry.get("mini-1")
    health_monitor.record_model_probe("mini-1", "gemma", healthy=False, at=clock())
    clock.advance(seconds=30)

    assert client.get("/health/ready").status_code == 200
    live = registry.get("mini-1")
    assert live is not original_host
    assert live.registration_id is original_host.registration_id
    assert registry.in_flight("mini-1") == 0
    assert health_monitor.has_model_trial("mini-1", "gemma") is False
    assert health_monitor.may_admit_model("mini-1", "gemma", clock()) is True


def test_proactive_probe_soft_reregister_mid_complete_does_not_settle():
    clock = _FakeClock()
    registry = HostRegistry(new_registry_db_path())

    class _SoftReregisterOnComplete:
        backend_id = "mini-1"
        health_timeout = 5.0

        def __init__(self) -> None:
            self.calls = 0

        async def check_health(self) -> BackendHealth:
            return BackendHealth(healthy=True, detail="ok")

        async def list_model_states(self, *, raise_on_timeout: bool = False):
            return [RouterModelState(id="gemma", state="loaded")]

        async def complete(self, request: ChatCompletionRequest) -> BackendResponse:
            self.calls += 1
            host = registry.get("mini-1")
            registry.register(
                "mini-1",
                replace(host.capabilities, base_url="http://mini-1.local:9090"),
                host.capacity,
                at=host.last_seen,
            )
            return BackendResponse(
                model=request.model,
                content="ok",
                finish_reason="stop",
                prompt_tokens=2,
                completion_tokens=1,
            )

    backend = _SoftReregisterOnComplete()
    health_monitor = HealthMonitor(failure_threshold=1, cooldown=timedelta(seconds=30))
    policy = RoutingPolicy(rules=[PolicyRule(name="flat", score_fn=lambda c, ctx: 0.0)])
    app = create_app(
        registry=registry,
        router=RoutingEngine(policy),
        health_monitor=health_monitor,
        scheduling_queue=SchedulingQueue(),
        backend_factories={"fake": lambda caps: backend},
        metrics_registry=MetricsRegistry(),
        alert_evaluator=AlertEvaluator([]),
        key_store=_permissive_key_store(),
        external_load_probe=inert_external_load_probe(),
        health_clock=clock,
        lease_clock=clock,
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
            "allowed_models": ["gemma"],
            "swap_mode": "router",
        },
    )
    original_host = registry.get("mini-1")
    health_monitor.record_model_probe("mini-1", "gemma", healthy=False, at=clock())
    clock.advance(seconds=30)

    assert client.get("/health/ready").status_code == 200
    assert backend.calls == 1
    live = registry.get("mini-1")
    assert live is not original_host
    assert live.registration_id is original_host.registration_id
    assert live.capabilities.base_url == "http://mini-1.local:9090"
    assert registry.in_flight("mini-1") == 0
    assert health_monitor.has_model_trial("mini-1", "gemma") is False
    assert health_monitor.is_model_healthy("mini-1", "gemma", clock()) is False
    assert health_monitor.may_admit_model("mini-1", "gemma", clock()) is True


def test_proactive_probe_lease_during_complete_does_not_settle():
    clock = _FakeClock()
    registry = HostRegistry(new_registry_db_path())
    lease_book = NodeLeaseBook(LeaseSettings(min_active_nodes=0))

    class _LeaseOnComplete:
        backend_id = "mini-1"
        health_timeout = 5.0

        def __init__(self) -> None:
            self.calls = 0

        async def check_health(self) -> BackendHealth:
            return BackendHealth(healthy=True, detail="ok")

        async def list_model_states(self, *, raise_on_timeout: bool = False):
            return [RouterModelState(id="gemma", state="loaded")]

        async def complete(self, request: ChatCompletionRequest) -> BackendResponse:
            self.calls += 1
            lease_book.reserve(
                holder="real-llm-check",
                hosts=[
                    ReservableHost(
                        host_id="mini-1",
                        status="online",
                        healthy=True,
                        serves_requested_model=True,
                    )
                ],
                host_id="mini-1",
                model=None,
                ceiling=None,
                now=clock(),
            )
            return BackendResponse(
                model=request.model,
                content="ok",
                finish_reason="stop",
                prompt_tokens=2,
                completion_tokens=1,
            )

    backend = _LeaseOnComplete()
    health_monitor = HealthMonitor(failure_threshold=1, cooldown=timedelta(seconds=30))
    policy = RoutingPolicy(rules=[PolicyRule(name="flat", score_fn=lambda c, ctx: 0.0)])
    app = create_app(
        registry=registry,
        router=RoutingEngine(policy),
        health_monitor=health_monitor,
        scheduling_queue=SchedulingQueue(),
        backend_factories={"fake": lambda caps: backend},
        metrics_registry=MetricsRegistry(),
        alert_evaluator=AlertEvaluator([]),
        key_store=_permissive_key_store(),
        external_load_probe=inert_external_load_probe(),
        health_clock=clock,
        lease_clock=clock,
        lease_book=lease_book,
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
            "allowed_models": ["gemma"],
            "swap_mode": "router",
        },
    )
    health_monitor.record_model_probe("mini-1", "gemma", healthy=False, at=clock())
    clock.advance(seconds=30)

    assert client.get("/health/ready").status_code == 200
    assert backend.calls == 1
    assert lease_book.is_held("mini-1", clock()) is True
    assert registry.in_flight("mini-1") == 0
    assert health_monitor.has_model_trial("mini-1", "gemma") is False
    assert health_monitor.is_model_healthy("mini-1", "gemma", clock()) is False
    assert health_monitor.may_admit_model("mini-1", "gemma", clock()) is True

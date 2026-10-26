import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from starlette.types import Receive

from llm_home_lab.api.models import ChatCompletionRequest
from llm_home_lab.backends.base import BackendError, BackendTimeoutError, ChatBackend
from llm_home_lab.health.monitor import HealthMonitor
from llm_home_lab.leases.book import NodeLeaseBook
from llm_home_lab.leases.models import (
    LEASE_HEADER,
    LeaseEndedError,
    LeaseInvalidError,
    LeaseNotFoundError,
    LeaseNotHeldError,
    LeaseRefusedError,
    LeaseSettings,
    NodeLease,
    ReservableHost,
)
from llm_home_lab.leases.path import rewrite_lease_path
from llm_home_lab.observability.alerts import AlertEvaluator
from llm_home_lab.observability.completion_log import CompletionLog
from llm_home_lab.observability.metrics import MetricsRegistry
from llm_home_lab.registry.external_load import ExternalLoadProbe, ExternalLoadStatus, LoadProbe
from llm_home_lab.registry.models import (
    HostCapabilities,
    HostCapacity,
    HostInfo,
    HostNotRegisteredError,
)
from llm_home_lab.registry.registry import HostRegistry
from llm_home_lab.routing.engine import RoutingEngine
from llm_home_lab.routing.models import NoAvailableBackendError, RoutingCandidate
from llm_home_lab.scheduling.queue import SchedulingQueue
from llm_home_lab.security.key_store import ApiKeyStore

access_logger = logging.getLogger("llm_home_lab.access")
audit_logger = logging.getLogger("llm_home_lab.audit")
health_logger = logging.getLogger("llm_home_lab.health")

AUTH_EXEMPT_PATHS = {"/health/live", "/health/ready", "/metrics"}


class ModelNotAvailableError(Exception):
    """No registered host serves the requested model."""


class ModelCapacityExceededError(Exception):
    """A host could serve the model but loading it now would exceed its memory budget."""


class NodeRegistrationRequest(BaseModel):
    host_id: str
    backend_type: str
    context_window: int
    base_url: str
    max_concurrent_requests: int
    allowed_models: list[str] | None = None
    memory_budget_gb: float | None = None
    model_sizes_gb: dict[str, float] | None = None
    model_aliases: dict[str, list[str]] | None = None
    position: int | None = None


class NodeLeaseCreateRequest(BaseModel):
    holder: str
    host_id: str | None = None
    model: str | None = None
    ceiling_s: int | None = None


class NodeLeaseForceReleaseRequest(BaseModel):
    host_id: str


class NodeUpdateRequest(BaseModel):
    backend_type: str | None = None
    context_window: int | None = None
    base_url: str | None = None
    max_concurrent_requests: int | None = None
    allowed_models: list[str] | None = None
    memory_budget_gb: float | None = None
    model_sizes_gb: dict[str, float] | None = None
    model_aliases: dict[str, list[str]] | None = None
    position: int | None = None


_NODE_CAPABILITY_FIELDS = {
    "backend_type",
    "context_window",
    "base_url",
    "allowed_models",
    "memory_budget_gb",
    "model_sizes_gb",
    "model_aliases",
    "position",
}
_NODE_CAPACITY_FIELDS = {"max_concurrent_requests"}


def _error_response(status_code: int, message: str, error_type: str, code: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "type": error_type, "code": code}},
    )


def create_app(
    registry: HostRegistry,
    router: RoutingEngine,
    health_monitor: HealthMonitor,
    scheduling_queue: SchedulingQueue,
    backend_factories: Mapping[str, Callable[[HostCapabilities], ChatBackend]],
    metrics_registry: MetricsRegistry,
    alert_evaluator: AlertEvaluator,
    key_store: ApiKeyStore | None = None,
    auth_enabled: bool = True,
    dispatch_wait_timeout: float = 30.0,
    dispatch_poll_interval: float = 0.1,
    external_load_probe: LoadProbe | None = None,
    health_poll_interval: float | None = None,
    completion_log: CompletionLog | None = None,
    lease_book: NodeLeaseBook | None = None,
    lease_settings: LeaseSettings | None = None,
    lease_clock: Callable[[], datetime] | None = None,
) -> FastAPI:
    if auth_enabled and key_store is None:
        raise ValueError("key_store is required when auth_enabled is True")

    external_load_probe = external_load_probe or ExternalLoadProbe()
    completion_log = completion_log or CompletionLog()
    lease_book = lease_book or NodeLeaseBook(lease_settings)

    def _lease_now() -> datetime:
        if lease_clock is None:
            return datetime.now(UTC)
        return lease_clock()

    backends_by_id: dict[str, ChatBackend] = {}
    backend_capabilities_by_id: dict[str, HostCapabilities] = {}

    def _backend_for(host_id: str, capabilities: HostCapabilities) -> ChatBackend:
        backend = backends_by_id.get(host_id)
        if backend is None or backend_capabilities_by_id[host_id] != capabilities:
            backend = backend_factories[capabilities.backend_type](capabilities)
            # The registry's host_id, not whatever the factory assigned, is the identifier
            # routing/health/scheduling key on everywhere else.
            backend.backend_id = host_id
            backends_by_id[host_id] = backend
            backend_capabilities_by_id[host_id] = capabilities
        return backend

    def _prune_backend_cache() -> None:
        live_ids = {host.host_id for host in registry.hosts()}
        for stale_id in set(backends_by_id) - live_ids:
            del backends_by_id[stale_id]
            del backend_capabilities_by_id[stale_id]
        for stale_id in set(external_loads) - live_ids:
            del external_loads[stale_id]

    def _fits_in_budget(host: HostInfo, model: str, loaded: list[str]) -> bool:
        budget = host.capabilities.memory_budget_gb
        if budget is None:
            return False  # no on-demand-loading budget configured: strict default applies

        sizes = host.capabilities.model_sizes_gb or {}
        requested_size = sizes.get(model)
        if requested_size is None:
            return False  # can't verify it fits: fail closed

        current_usage = 0.0
        for loaded_model in loaded:
            size = sizes.get(loaded_model)
            if size is None:
                return False  # can't verify current headroom: fail closed
            current_usage += size

        return current_usage + requested_size <= budget

    async def _model_capable_hosts(model: str) -> tuple[list[HostInfo], bool]:
        capable = []
        any_budget_blocked = False
        for host in registry.hosts():
            allowed_models = host.capabilities.allowed_models
            if allowed_models is not None:
                if model in allowed_models:
                    capable.append(host)
                continue

            backend = _backend_for(host.host_id, host.capabilities)
            list_models = getattr(backend, "list_models", None)
            if list_models is None:
                capable.append(host)
                continue

            loaded = await list_models()
            if loaded is None or model in loaded:
                capable.append(host)
            elif _fits_in_budget(host, model, loaded):
                capable.append(host)
            elif host.capabilities.memory_budget_gb is not None:
                any_budget_blocked = True
        return capable, any_budget_blocked

    def _node_status(host_id: str, at: datetime) -> str:
        if not health_monitor.has_probe_history(host_id):
            return "unknown"
        return "online" if health_monitor.is_healthy(host_id, at) else "offline"

    def _node_health(host_id: str) -> dict[str, int]:
        return {"recent_failures": health_monitor.failure_count(host_id)}

    def _eligible_candidates(hosts: list[HostInfo], at: datetime) -> list[RoutingCandidate]:
        candidates = []
        for host in hosts:
            if not health_monitor.is_healthy(host.host_id, at):
                continue
            if host.in_flight >= host.capacity.max_concurrent_requests:
                continue
            # No real latency measurement yet (see routing spec's Open Questions), so use
            # each host's current load relative to its own capacity as a least-connections
            # proxy — otherwise every request ties and falls back to alphabetical backend_id,
            # filling one host before ever trying the others.
            load_ratio = host.in_flight / host.capacity.max_concurrent_requests
            candidates.append(
                RoutingCandidate(
                    backend=_backend_for(host.host_id, host.capabilities),
                    latency_ms=load_ratio,
                    context_window=host.capabilities.context_window,
                )
            )
        return candidates

    async def _reservable_hosts(model: str | None) -> list[ReservableHost]:
        capable_ids: set[str] | None
        if model is None:
            capable_ids = None
        else:
            capable, _budget_blocked = await _model_capable_hosts(model)
            capable_ids = {host.host_id for host in capable}
        now = _lease_now()
        return [
            ReservableHost(
                host_id=host.host_id,
                status=_node_status(host.host_id, now),
                healthy=health_monitor.is_healthy(host.host_id, now),
                serves_requested_model=capable_ids is None or host.host_id in capable_ids,
            )
            for host in registry.hosts()
        ]

    # Last external-load result per host, written only by _probe_all_hosts. /v1/nodes serves
    # this instead of probing inline so a slow host (`lms ps --host` can take its full timeout)
    # never delays the dashboard's request; a host not probed yet reports "unavailable".
    external_loads: dict[str, ExternalLoadStatus] = {}

    async def _probe_one_host(host: HostInfo) -> dict[str, object]:
        backend = _backend_for(host.host_id, host.capabilities)
        health = await backend.check_health()
        health_monitor.record_probe(host.host_id, health.healthy, datetime.now(UTC))
        external_loads[host.host_id] = await external_load_probe.probe(
            host.host_id, host.capabilities.base_url, datetime.now(UTC)
        )
        return {"id": host.host_id, "healthy": health.healthy, "detail": health.detail}

    async def _probe_all_hosts() -> list[dict[str, object]]:
        """Probe every registered host once, recording results into health_monitor and
        external_load_probe. Shared by /health/ready (on-demand) and the background poll
        loop below (continuous) so health state never depends on which of the two — or
        whether either — happens to run (see docs/adr/0006-background-health-poller.md).

        Hosts are probed concurrently, not sequentially: each backend's check_health() is
        already individually bounded (see health_timeout in llamaserver.py/lmstudio.py), but
        one dead-on-arrival host still shouldn't delay every other host's probe behind it —
        with N unreachable hosts, a sequential loop pays N times the per-host timeout instead
        of paying it once.
        """
        _prune_backend_cache()
        reports = list(await asyncio.gather(*(_probe_one_host(host) for host in registry.hosts())))
        alert_now = datetime.now(UTC)
        alert_evaluator.evaluate(
            metrics_registry.snapshot(alert_now, registry, scheduling_queue), alert_now
        )
        return reports

    async def _health_poll_loop(interval: float) -> None:
        while True:
            try:
                await _probe_all_hosts()
            except Exception:
                # One bad tick (a host timing out, an unexpected error) must not kill the
                # loop — that would silently reintroduce the exact bug this poller fixes.
                health_logger.exception("background health poll tick failed; retrying")
            await asyncio.sleep(interval)

    @contextlib.asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        task: asyncio.Task[None] | None = None
        if health_poll_interval is not None:
            task = asyncio.create_task(_health_poll_loop(health_poll_interval))
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(lifespan=_lifespan)
    app.state.registry = registry
    app.state.router = router
    app.state.health_monitor = health_monitor
    app.state.scheduling_queue = scheduling_queue
    app.state.auth_enabled = auth_enabled
    app.state.dispatch_wait_timeout = dispatch_wait_timeout
    app.state.external_load_probe = external_load_probe

    @app.middleware("http")
    async def log_requests(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        start = time.perf_counter()

        response = await call_next(request)

        latency_ms = (time.perf_counter() - start) * 1000
        response.headers["X-Request-ID"] = request_id
        access_logger.info(
            "request_id=%s method=%s path=%s status=%d latency_ms=%.2f",
            request_id,
            request.method,
            request.url.path,
            response.status_code,
            latency_ms,
        )
        metrics_registry.record_request(
            request.url.path, response.status_code, latency_ms, datetime.now(UTC)
        )
        return response

    @app.middleware("http")
    async def enforce_auth(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if not auth_enabled:
            return await call_next(request)
        assert key_store is not None  # enforced by the create_app entry check above

        path = request.url.path
        if path in AUTH_EXEMPT_PATHS:
            return await call_next(request)

        auth_header = request.headers.get("authorization", "")
        token = auth_header.removeprefix("Bearer ") if auth_header.startswith("Bearer ") else None
        identity = key_store.authenticate(token, datetime.now(UTC)) if token else None

        if identity is None:
            reason = "missing_token" if token is None else "invalid_token"
            audit_logger.info(
                "client_id=%s method=%s path=%s outcome=%s reason=%s",
                "unknown",
                request.method,
                path,
                "blocked",
                reason,
            )
            return _error_response(
                401, "missing or invalid API key", "invalid_request_error", reason
            )

        if not key_store.is_authorized(identity, path):
            audit_logger.info(
                "client_id=%s method=%s path=%s outcome=%s reason=%s",
                identity.client_id,
                request.method,
                path,
                "blocked",
                "path_not_allowed",
            )
            return _error_response(
                403,
                "client is not authorized for this path",
                "invalid_request_error",
                "path_not_allowed",
            )

        audit_logger.info(
            "client_id=%s method=%s path=%s outcome=%s reason=%s",
            identity.client_id,
            request.method,
            path,
            "allowed",
            "ok",
        )
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def on_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _error_response(400, str(exc.errors()), "invalid_request_error", "invalid_request")

    @app.exception_handler(BackendTimeoutError)
    async def on_backend_timeout(request: Request, exc: BackendTimeoutError) -> JSONResponse:
        return _error_response(504, str(exc), "backend_error", "backend_timeout")

    @app.exception_handler(BackendError)
    async def on_backend_error(request: Request, exc: BackendError) -> JSONResponse:
        return _error_response(503, str(exc), "backend_error", "backend_unavailable")

    @app.exception_handler(NoAvailableBackendError)
    async def on_no_available_backend(
        request: Request, exc: NoAvailableBackendError
    ) -> JSONResponse:
        return _error_response(503, str(exc), "backend_error", "no_available_backend")

    @app.exception_handler(HostNotRegisteredError)
    async def on_host_not_registered(request: Request, exc: HostNotRegisteredError) -> JSONResponse:
        return _error_response(404, str(exc), "invalid_request_error", "host_not_registered")

    @app.exception_handler(ModelNotAvailableError)
    async def on_model_not_available(request: Request, exc: ModelNotAvailableError) -> JSONResponse:
        return _error_response(400, str(exc), "invalid_request_error", "model_not_available")

    @app.exception_handler(ModelCapacityExceededError)
    async def on_model_capacity_exceeded(
        request: Request, exc: ModelCapacityExceededError
    ) -> JSONResponse:
        return _error_response(503, str(exc), "backend_error", "model_capacity_exceeded")

    @app.exception_handler(LeaseRefusedError)
    async def on_lease_refused(request: Request, exc: LeaseRefusedError) -> JSONResponse:
        return _error_response(409, str(exc), "invalid_request_error", "lease_refused")

    @app.exception_handler(LeaseInvalidError)
    async def on_lease_invalid(request: Request, exc: LeaseInvalidError) -> JSONResponse:
        return _error_response(400, str(exc), "invalid_request_error", "lease_invalid")

    @app.exception_handler(LeaseNotHeldError)
    async def on_lease_not_held(request: Request, exc: LeaseNotHeldError) -> JSONResponse:
        return _error_response(403, str(exc), "invalid_request_error", "lease_not_held")

    @app.exception_handler(LeaseEndedError)
    async def on_lease_ended(request: Request, exc: LeaseEndedError) -> JSONResponse:
        return _error_response(409, str(exc), "invalid_request_error", "lease_ended")

    @app.exception_handler(LeaseNotFoundError)
    async def on_lease_not_found(request: Request, exc: LeaseNotFoundError) -> JSONResponse:
        return _error_response(404, str(exc), "invalid_request_error", "lease_not_found")

    @app.post("/v1/nodes/register")
    async def register_node(payload: NodeRegistrationRequest) -> dict[str, str]:
        registry.register(
            payload.host_id,
            HostCapabilities(
                backend_type=payload.backend_type,
                context_window=payload.context_window,
                base_url=payload.base_url,
                allowed_models=payload.allowed_models,
                memory_budget_gb=payload.memory_budget_gb,
                model_sizes_gb=payload.model_sizes_gb,
                model_aliases=payload.model_aliases,
                position=payload.position,
            ),
            HostCapacity(max_concurrent_requests=payload.max_concurrent_requests),
            at=datetime.now(UTC),
        )
        return {"status": "registered"}

    @app.patch("/v1/nodes/{host_id:path}")
    async def update_node(host_id: str, payload: NodeUpdateRequest) -> dict[str, str]:
        existing = registry.get(host_id)
        fields = payload.model_dump(exclude_unset=True)
        capabilities = replace(
            existing.capabilities,
            **{k: v for k, v in fields.items() if k in _NODE_CAPABILITY_FIELDS},
        )
        capacity = replace(
            existing.capacity,
            **{k: v for k, v in fields.items() if k in _NODE_CAPACITY_FIELDS},
        )
        registry.register(host_id, capabilities, capacity, at=datetime.now(UTC))
        return {"status": "updated"}

    @app.post("/v1/nodes/{host_id:path}/heartbeat")
    async def heartbeat_node(host_id: str) -> dict[str, str]:
        registry.heartbeat(host_id, at=datetime.now(UTC))
        return {"status": "ok"}

    @app.delete("/v1/nodes/{host_id:path}")
    async def deregister_node(host_id: str) -> dict[str, str]:
        registry.deregister(host_id)
        _prune_backend_cache()
        return {"status": "deregistered"}

    def _lease_body(lease: NodeLease, now: datetime, *, include_token: bool) -> dict[str, object]:
        remaining = min(lease.expires_at, lease.hard_expires_at) - now
        return lease.public_dict(
            include_token=include_token,
            expiring_soon=remaining <= lease_book.settings.expiring_soon,
        )

    @app.post("/v1/node-leases", status_code=201)
    async def create_node_lease(payload: NodeLeaseCreateRequest) -> dict[str, object]:
        hosts = await _reservable_hosts(payload.model)
        now = _lease_now()
        ceiling = None if payload.ceiling_s is None else timedelta(seconds=payload.ceiling_s)
        lease = lease_book.reserve(
            holder=payload.holder,
            hosts=hosts,
            host_id=payload.host_id,
            model=payload.model,
            ceiling=ceiling,
            now=now,
        )
        return _lease_body(lease, now, include_token=True)

    @app.post("/v1/node-leases/heartbeat")
    async def heartbeat_node_lease(http_request: Request) -> dict[str, object]:
        now = _lease_now()
        lease = lease_book.heartbeat(http_request.headers.get(LEASE_HEADER, ""), now)
        return _lease_body(lease, now, include_token=False)

    @app.delete("/v1/node-leases")
    async def release_node_lease(http_request: Request) -> dict[str, str]:
        lease_book.release(http_request.headers.get(LEASE_HEADER, ""), _lease_now())
        return {"status": "released"}

    @app.post("/v1/node-leases/force-release")
    async def force_release_node_lease(payload: NodeLeaseForceReleaseRequest) -> dict[str, str]:
        lease_book.force_release(payload.host_id, _lease_now())
        return {"status": "force_released"}

    @app.get("/v1/nodes")
    async def list_nodes() -> dict[str, list[dict[str, object]]]:
        at = datetime.now(UTC)
        lease_at = _lease_now()
        # sorted() is stable, so unpositioned nodes keep registration order after positioned ones.
        hosts = sorted(
            registry.hosts(),
            key=lambda h: (h.capabilities.position is None, h.capabilities.position or 0),
        )
        unprobed = ExternalLoadStatus(available=False, status=None, queued=None, checked_at=at)
        return {
            "nodes": [
                {
                    "host_id": host.host_id,
                    "backend_type": host.capabilities.backend_type,
                    "context_window": host.capabilities.context_window,
                    "base_url": host.capabilities.base_url,
                    "allowed_models": host.capabilities.allowed_models,
                    "memory_budget_gb": host.capabilities.memory_budget_gb,
                    "model_sizes_gb": host.capabilities.model_sizes_gb,
                    "model_aliases": host.capabilities.model_aliases,
                    "position": host.capabilities.position,
                    "max_concurrent_requests": host.capacity.max_concurrent_requests,
                    "in_flight": host.in_flight,
                    "last_seen": host.last_seen.isoformat(),
                    "status": _node_status(host.host_id, at),
                    "health": _node_health(host.host_id),
                    "external_load": {
                        "available": load.available,
                        "status": load.status,
                        "queued": load.queued,
                        "total_slots": load.total_slots,
                        "busy_slots": load.busy_slots,
                    },
                    "throughput": {
                        "window_s": completion_log.window.total_seconds(),
                        "tasks_per_hour": completion_log.tasks_per_hour(host.host_id, at),
                        "tasks_per_hour_per_slot": completion_log.tasks_per_hour_per_slot(
                            host.host_id, at, load.total_slots
                        ),
                    },
                    "lease": lease_book.view(host.host_id, lease_at),
                }
                for host in hosts
                for load in [external_loads.get(host.host_id, unprobed)]
            ]
        }

    @app.get("/v1/capacity")
    async def get_capacity() -> dict[str, int]:
        at = datetime.now(UTC)
        total = sum(
            host.capacity.max_concurrent_requests
            for host in registry.hosts()
            if _node_status(host.host_id, at) == "online"
        )
        return {"total_max_concurrent_requests": total}

    @app.get("/v1/alerts")
    async def list_alerts() -> dict[str, list[dict[str, object]]]:
        return {
            "alerts": [
                {
                    "rule_name": event.rule_name,
                    "severity": event.severity,
                    "state": event.state,
                    "value": event.value,
                    "threshold_value": event.threshold_value,
                    "runbook_url": event.runbook_url,
                    "at": event.at.isoformat(),
                }
                for event in alert_evaluator.current_state()
            ]
        }

    @app.get("/metrics")
    async def metrics() -> Response:
        body = metrics_registry.render_prometheus(datetime.now(UTC), registry, scheduling_queue)
        return Response(content=body, media_type="text/plain; version=0.0.4")

    @app.get("/health/live")
    async def health_live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def health_ready() -> JSONResponse:
        reports = await _probe_all_hosts()
        all_healthy = all(report["healthy"] for report in reports)
        return JSONResponse(
            status_code=200 if all_healthy else 503,
            content={
                "status": "ok" if all_healthy else "unavailable",
                "backends": reports,
            },
        )

    @app.post("/v1/chat/completions", response_model=None)
    async def chat_completions(
        request: ChatCompletionRequest,
        response: Response,
        http_request: Request,
    ) -> StreamingResponse | dict[str, object]:
        now = datetime.now(UTC)
        lease_now = _lease_now()
        token = http_request.headers.get(LEASE_HEADER)
        pinned = lease_book.require(token, lease_now) if token else None
        model_hosts, budget_blocked = await _model_capable_hosts(request.model)
        lease_now = _lease_now()
        pinned = lease_book.require(token, lease_now) if token else None
        all_model_hosts = model_hosts
        if pinned is not None:
            # A leased request stays on that one node. Model matching is unchanged,
            # and a miss does not fall through to any other host.
            model_hosts = [host for host in model_hosts if host.host_id == pinned.host_id]
            if not model_hosts:
                raise ModelNotAvailableError(
                    f"leased node {pinned.host_id!r} does not serve model {request.model!r}"
                )
        else:
            open_hosts = []
            leased_any = False
            for host in model_hosts:
                if lease_book.is_held(host.host_id, lease_now):
                    leased_any = True
                    continue
                open_hosts.append(host)
            if not open_hosts:
                if leased_any:
                    raise NoAvailableBackendError(
                        "every host that serves this model is reserved for a test"
                    )
                if budget_blocked:
                    raise ModelCapacityExceededError(
                        f"model {request.model!r} would exceed a host's memory budget right now"
                    )
                raise ModelNotAvailableError(f"no registered host serves model {request.model!r}")
            model_hosts = open_hosts

        failover_in_play = any(
            not health_monitor.is_healthy(host.host_id, now) for host in model_hosts
        )

        def current_candidates() -> list[RoutingCandidate]:
            at = _lease_now()
            live = lease_book.require(token, at) if token else None
            hosts = [
                host
                for host in all_model_hosts
                if (
                    host.host_id == live.host_id
                    if live
                    else not lease_book.is_held(host.host_id, at)
                )
            ]
            return _eligible_candidates(hosts, datetime.now(UTC))

        def queued_host_ids() -> set[str]:
            try:
                return {candidate.backend.backend_id for candidate in current_candidates()}
            except (LeaseEndedError, LeaseNotHeldError):
                return set()

        candidates = current_candidates()
        if not candidates:
            request_id = uuid.uuid4().hex
            queue_session_id = request.session_id or request_id
            waited = 0.0
            scheduling_queue.enqueue(
                request_id,
                session_id=queue_session_id,
                priority=0,
                at=now,
                eligible_host_ids=queued_host_ids,
            )
            try:
                while waited < dispatch_wait_timeout:
                    await asyncio.sleep(dispatch_poll_interval)
                    waited += dispatch_poll_interval
                    candidates = current_candidates()
                    if (
                        candidates
                        and scheduling_queue.dispatch(
                            registry,
                            datetime.now(UTC),
                            available_host_ids={c.backend.backend_id for c in candidates},
                            request_id=request_id,
                        )
                        == request_id
                    ):
                        break
                else:
                    if failover_in_play:
                        metrics_registry.record_failover_outcome(False, datetime.now(UTC))
                    raise NoAvailableBackendError(
                        "no host became available before the dispatch timeout"
                    )
            finally:
                scheduling_queue.cancel(request_id, session_id=queue_session_id, priority=0)

        decision = router.select_backend(request, candidates, session_id=request.session_id)
        backend = backends_by_id[decision.backend_id]
        registry.acquire_slot(decision.backend_id)

        if request.stream:
            if failover_in_play:
                metrics_registry.record_failover_outcome(True, datetime.now(UTC))

            async def _chunks() -> AsyncIterator[str]:
                try:
                    async for chunk in _stream_chunks(
                        backend,
                        request,
                        metrics_registry,
                        completion_log,
                        health_monitor,
                        decision.backend_id,
                    ):
                        yield chunk
                finally:
                    registry.release_slot(decision.backend_id)

            return StreamingResponse(
                _chunks(),
                media_type="text/event-stream",
                headers={"X-Backend-Id": decision.backend_id},
            )

        backend_call_start = time.perf_counter()
        try:
            result = await backend.complete(request)
        except BackendError:
            if failover_in_play:
                metrics_registry.record_failover_outcome(False, datetime.now(UTC))
            health_monitor.record_probe(decision.backend_id, healthy=False, at=datetime.now(UTC))
            raise
        finally:
            registry.release_slot(decision.backend_id)
        backend_call_latency_ms = (time.perf_counter() - backend_call_start) * 1000

        if failover_in_play:
            metrics_registry.record_failover_outcome(True, datetime.now(UTC))
        metrics_registry.record_token_usage(
            decision.backend_id, result.prompt_tokens, result.completion_tokens, datetime.now(UTC)
        )
        is_healthy_completion = not _is_degenerate_completion(result.content, result.finish_reason)
        if is_healthy_completion:
            metrics_registry.record_host_completion(
                decision.backend_id,
                backend_call_latency_ms,
                result.prompt_tokens,
                result.completion_tokens,
                datetime.now(UTC),
            )
            completion_log.record(decision.backend_id, datetime.now(UTC))
        health_monitor.record_probe(
            decision.backend_id, healthy=is_healthy_completion, at=datetime.now(UTC)
        )

        response.headers["X-Backend-Id"] = decision.backend_id
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": result.model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": result.content},
                    "finish_reason": result.finish_reason,
                }
            ],
            "usage": {
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
                "total_tokens": result.prompt_tokens + result.completion_tokens,
            },
        }

    class _LeasePathMiddleware:
        """Outermost on purpose: auth and routing see the path with the token removed."""

        def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
            self._app = app

        async def __call__(
            self,
            scope: dict[str, object],
            receive: Receive,
            send: Callable[[object], Awaitable[None]],
        ) -> None:
            if scope.get("type") == "http":
                rewrite_lease_path(scope)
                path = scope.get("path")
                if isinstance(path, str) and path.startswith("/lease/"):
                    await Response(status_code=404)(scope, receive, send)
                    return
            await self._app(scope, receive, send)

    app.add_middleware(_LeasePathMiddleware)
    return app


def _is_degenerate_completion(content: str, finish_reason: str | None) -> bool:
    return not content.strip() or finish_reason != "stop"


async def _stream_chunks(
    backend: ChatBackend,
    request: ChatCompletionRequest,
    metrics_registry: MetricsRegistry,
    completion_log: CompletionLog,
    health_monitor: HealthMonitor,
    backend_id: str,
) -> AsyncIterator[str]:
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    usage: dict[str, int] | None = None
    saw_content = False
    last_finish_reason: str | None = None

    stream_start = time.perf_counter()
    async for chunk in backend.stream(request):
        if chunk.usage is not None:
            usage = chunk.usage
        if chunk.content.strip():
            saw_content = True
        if chunk.finish_reason is not None:
            last_finish_reason = chunk.finish_reason
        if not chunk.content and chunk.finish_reason is None:
            # A usage-only accounting chunk (no content, no finish_reason) — consumed for
            # metrics below, not forwarded as a delta to the external client.
            continue

        payload = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": request.model,
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": chunk.content},
                    "finish_reason": chunk.finish_reason,
                }
            ],
        }
        yield f"data: {json.dumps(payload)}\n\n"

    stream_latency_ms = (time.perf_counter() - stream_start) * 1000
    is_healthy_completion = saw_content and last_finish_reason == "stop"
    if usage is not None:
        metrics_registry.record_token_usage(
            backend_id, usage["prompt_tokens"], usage["completion_tokens"], datetime.now(UTC)
        )
        if is_healthy_completion:
            metrics_registry.record_host_completion(
                backend_id,
                stream_latency_ms,
                usage["prompt_tokens"],
                usage["completion_tokens"],
                datetime.now(UTC),
            )
    if is_healthy_completion:
        completion_log.record(backend_id, datetime.now(UTC))
    health_monitor.record_probe(backend_id, healthy=is_healthy_completion, at=datetime.now(UTC))

    yield "data: [DONE]\n\n"

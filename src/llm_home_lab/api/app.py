import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import UTC, datetime

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from llm_home_lab.api.models import ChatCompletionRequest
from llm_home_lab.backends.base import BackendError, BackendTimeoutError, ChatBackend
from llm_home_lab.health.monitor import HealthMonitor
from llm_home_lab.observability.alerts import AlertEvaluator
from llm_home_lab.observability.metrics import MetricsRegistry
from llm_home_lab.registry.external_load import ExternalLoadProbe
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
    external_load_probe: ExternalLoadProbe | None = None,
) -> FastAPI:
    if auth_enabled and key_store is None:
        raise ValueError("key_store is required when auth_enabled is True")

    external_load_probe = external_load_probe or ExternalLoadProbe()
    app = FastAPI()
    app.state.registry = registry
    app.state.router = router
    app.state.health_monitor = health_monitor
    app.state.scheduling_queue = scheduling_queue
    app.state.auth_enabled = auth_enabled
    app.state.dispatch_wait_timeout = dispatch_wait_timeout
    app.state.external_load_probe = external_load_probe
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

    def _eligible_candidates(hosts: list[HostInfo], at: datetime) -> list[RoutingCandidate]:
        candidates = []
        for host in hosts:
            if not health_monitor.is_healthy(host.host_id, at):
                continue
            if host.in_flight >= host.capacity.max_concurrent_requests:
                continue
            candidates.append(
                RoutingCandidate(
                    backend=_backend_for(host.host_id, host.capabilities),
                    latency_ms=0.0,
                    context_window=host.capabilities.context_window,
                )
            )
        return candidates

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
            ),
            HostCapacity(max_concurrent_requests=payload.max_concurrent_requests),
            at=datetime.now(UTC),
        )
        return {"status": "registered"}

    @app.post("/v1/nodes/{host_id:path}/heartbeat")
    async def heartbeat_node(host_id: str) -> dict[str, str]:
        registry.heartbeat(host_id, at=datetime.now(UTC))
        return {"status": "ok"}

    @app.delete("/v1/nodes/{host_id:path}")
    async def deregister_node(host_id: str) -> dict[str, str]:
        registry.deregister(host_id)
        _prune_backend_cache()
        return {"status": "deregistered"}

    @app.get("/v1/nodes")
    async def list_nodes() -> dict[str, list[dict[str, object]]]:
        at = datetime.now(UTC)
        hosts = registry.hosts()
        external_loads = await asyncio.gather(
            *(
                external_load_probe.probe(host.host_id, host.capabilities.base_url, at)
                for host in hosts
            )
        )
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
                    "max_concurrent_requests": host.capacity.max_concurrent_requests,
                    "in_flight": host.in_flight,
                    "last_seen": host.last_seen.isoformat(),
                    "status": _node_status(host.host_id, at),
                    "external_load": {
                        "available": load.available,
                        "status": load.status,
                        "queued": load.queued,
                    },
                }
                for host, load in zip(hosts, external_loads, strict=True)
            ]
        }

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
        _prune_backend_cache()
        reports = []
        for host in registry.hosts():
            backend = _backend_for(host.host_id, host.capabilities)
            health = await backend.check_health()
            health_monitor.record_probe(host.host_id, health.healthy, datetime.now(UTC))
            reports.append({"id": host.host_id, "healthy": health.healthy, "detail": health.detail})
            await external_load_probe.probe(
                host.host_id, host.capabilities.base_url, datetime.now(UTC)
            )
        all_healthy = all(report["healthy"] for report in reports)
        alert_now = datetime.now(UTC)
        alert_evaluator.evaluate(
            metrics_registry.snapshot(alert_now, registry, scheduling_queue), alert_now
        )
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
    ) -> StreamingResponse | dict[str, object]:
        now = datetime.now(UTC)
        model_hosts, budget_blocked = await _model_capable_hosts(request.model)
        if not model_hosts:
            if budget_blocked:
                raise ModelCapacityExceededError(
                    f"model {request.model!r} would exceed a host's memory budget right now"
                )
            raise ModelNotAvailableError(f"no registered host serves model {request.model!r}")

        failover_in_play = any(
            not health_monitor.is_healthy(host.host_id, now) for host in model_hosts
        )

        candidates = _eligible_candidates(model_hosts, now)

        if not candidates:
            request_id = uuid.uuid4().hex
            queue_session_id = request.session_id or request_id
            scheduling_queue.enqueue(request_id, session_id=queue_session_id, priority=0, at=now)
            waited = 0.0
            admitted = False
            while waited < dispatch_wait_timeout:
                await asyncio.sleep(dispatch_poll_interval)
                waited += dispatch_poll_interval
                if scheduling_queue.dispatch(registry, datetime.now(UTC)) == request_id:
                    admitted = True
                    break
            if not admitted:
                scheduling_queue.cancel(request_id, session_id=queue_session_id, priority=0)
                if failover_in_play:
                    metrics_registry.record_failover_outcome(False, datetime.now(UTC))
                raise NoAvailableBackendError(
                    "no host became available before the dispatch timeout"
                )
            candidates = _eligible_candidates(model_hosts, datetime.now(UTC))

        decision = router.select_backend(request, candidates, session_id=request.session_id)
        backend = backends_by_id[decision.backend_id]
        registry.acquire_slot(decision.backend_id)

        if request.stream:
            if failover_in_play:
                metrics_registry.record_failover_outcome(True, datetime.now(UTC))

            async def _chunks() -> AsyncIterator[str]:
                try:
                    async for chunk in _stream_chunks(
                        backend, request, metrics_registry, decision.backend_id
                    ):
                        yield chunk
                finally:
                    registry.release_slot(decision.backend_id)

            return StreamingResponse(_chunks(), media_type="text/event-stream")

        try:
            result = await backend.complete(request)
        except BackendError:
            if failover_in_play:
                metrics_registry.record_failover_outcome(False, datetime.now(UTC))
            raise
        finally:
            registry.release_slot(decision.backend_id)

        if failover_in_play:
            metrics_registry.record_failover_outcome(True, datetime.now(UTC))
        metrics_registry.record_token_usage(
            decision.backend_id, result.prompt_tokens, result.completion_tokens, datetime.now(UTC)
        )

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

    return app


async def _stream_chunks(
    backend: ChatBackend,
    request: ChatCompletionRequest,
    metrics_registry: MetricsRegistry,
    backend_id: str,
) -> AsyncIterator[str]:
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    usage: dict[str, int] | None = None

    async for chunk in backend.stream(request):
        if chunk.usage is not None:
            usage = chunk.usage
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

    if usage is not None:
        metrics_registry.record_token_usage(
            backend_id, usage["prompt_tokens"], usage["completion_tokens"], datetime.now(UTC)
        )

    yield "data: [DONE]\n\n"

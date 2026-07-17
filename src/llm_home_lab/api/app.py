import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import UTC, datetime

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse

from llm_home_lab.api.models import ChatCompletionRequest
from llm_home_lab.backends.base import BackendTimeoutError, ChatBackend
from llm_home_lab.health.monitor import HealthMonitor
from llm_home_lab.routing.engine import RoutingEngine
from llm_home_lab.routing.models import NoAvailableBackendError, RoutingCandidate

access_logger = logging.getLogger("llm_home_lab.access")


def _error_response(status_code: int, message: str, error_type: str, code: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "type": error_type, "code": code}},
    )


def create_app(
    candidates: Sequence[RoutingCandidate], router: RoutingEngine, health_monitor: HealthMonitor
) -> FastAPI:
    app = FastAPI()
    app.state.candidates = candidates
    app.state.router = router
    app.state.health_monitor = health_monitor
    backends_by_id: dict[str, ChatBackend] = {c.backend.backend_id: c.backend for c in candidates}

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
        return response

    @app.exception_handler(RequestValidationError)
    async def on_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _error_response(400, str(exc.errors()), "invalid_request_error", "invalid_request")

    @app.exception_handler(BackendTimeoutError)
    async def on_backend_timeout(request: Request, exc: BackendTimeoutError) -> JSONResponse:
        return _error_response(504, str(exc), "backend_error", "backend_timeout")

    @app.exception_handler(NoAvailableBackendError)
    async def on_no_available_backend(
        request: Request, exc: NoAvailableBackendError
    ) -> JSONResponse:
        return _error_response(503, str(exc), "backend_error", "no_available_backend")

    @app.get("/health/live")
    async def health_live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def health_ready() -> JSONResponse:
        reports = []
        for candidate in candidates:
            health = await candidate.backend.check_health()
            health_monitor.record_probe(
                candidate.backend.backend_id, health.healthy, datetime.now(UTC)
            )
            reports.append(
                {
                    "id": candidate.backend.backend_id,
                    "healthy": health.healthy,
                    "detail": health.detail,
                }
            )
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
    ) -> StreamingResponse | dict[str, object]:
        now = datetime.now(UTC)
        healthy_candidates = [
            c for c in candidates if health_monitor.is_healthy(c.backend.backend_id, now)
        ]
        decision = router.select_backend(request, healthy_candidates, session_id=request.session_id)
        backend = backends_by_id[decision.backend_id]

        if request.stream:
            return StreamingResponse(
                _stream_chunks(backend, request),
                media_type="text/event-stream",
            )

        result = await backend.complete(request)

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
    backend: ChatBackend, request: ChatCompletionRequest
) -> AsyncIterator[str]:
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())

    async for chunk in backend.stream(request):
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

    yield "data: [DONE]\n\n"

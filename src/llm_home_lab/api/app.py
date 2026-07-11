import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse

from llm_home_lab.api.models import ChatCompletionRequest
from llm_home_lab.backends.base import BackendTimeoutError, ChatBackend

access_logger = logging.getLogger("llm_home_lab.access")


def _error_response(status_code: int, message: str, error_type: str, code: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "type": error_type, "code": code}},
    )


def create_app(backend: ChatBackend) -> FastAPI:
    app = FastAPI()
    app.state.backend = backend

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

    @app.get("/health/live")
    async def health_live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def health_ready() -> JSONResponse:
        health = await backend.check_health()
        return JSONResponse(
            status_code=200 if health.healthy else 503,
            content={
                "status": "ok" if health.healthy else "unavailable",
                "backends": [
                    {
                        "id": backend.backend_id,
                        "healthy": health.healthy,
                        "detail": health.detail,
                    }
                ],
            },
        )

    @app.post("/v1/chat/completions", response_model=None)
    async def chat_completions(
        request: ChatCompletionRequest,
    ) -> StreamingResponse | dict[str, object]:
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

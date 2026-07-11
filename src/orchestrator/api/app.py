import json
import time
import uuid
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse

from orchestrator.api.models import ChatCompletionRequest
from orchestrator.backends.base import BackendTimeoutError, ChatBackend


def _error_response(status_code: int, message: str, error_type: str, code: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "type": error_type, "code": code}},
    )


def create_app(backend: ChatBackend) -> FastAPI:
    app = FastAPI()

    @app.exception_handler(RequestValidationError)
    async def on_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _error_response(400, str(exc.errors()), "invalid_request_error", "invalid_request")

    @app.exception_handler(BackendTimeoutError)
    async def on_backend_timeout(request: Request, exc: BackendTimeoutError) -> JSONResponse:
        return _error_response(504, str(exc), "backend_error", "backend_timeout")

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

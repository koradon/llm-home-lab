import json
import logging
from collections.abc import AsyncIterator

import httpx

from orchestrator.api.models import ChatCompletionRequest
from orchestrator.backends.base import (
    BackendChunk,
    BackendConnectionError,
    BackendHealth,
    BackendResponse,
    BackendResponseError,
    BackendTimeoutError,
)

logger = logging.getLogger(__name__)


class LMStudioBackend:
    def __init__(
        self,
        base_url: str,
        timeout: float,
        max_retries: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.backend_id = base_url
        self._max_retries = max_retries
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout, transport=transport)

    async def check_health(self) -> BackendHealth:
        try:
            response = await self._client.get("/v1/models")
        except httpx.TransportError as exc:
            return BackendHealth(healthy=False, detail=str(exc))

        if response.status_code // 100 != 2:
            return BackendHealth(
                healthy=False, detail=f"status {response.status_code}: {response.text}"
            )
        return BackendHealth(healthy=True, detail="ok")

    async def complete(self, request: ChatCompletionRequest) -> BackendResponse:
        response = await self._request_with_retry(request)
        if response.status_code // 100 != 2:
            raise BackendResponseError(response.status_code, response.text)

        body = response.json()
        choice = body["choices"][0]

        return BackendResponse(
            model=body["model"],
            content=choice["message"]["content"],
            finish_reason=choice["finish_reason"],
            prompt_tokens=body["usage"]["prompt_tokens"],
            completion_tokens=body["usage"]["completion_tokens"],
        )

    async def stream(self, request: ChatCompletionRequest) -> AsyncIterator[BackendChunk]:
        payload = _to_lmstudio_payload(request, stream=True)

        async with self._client.stream("POST", "/v1/chat/completions", json=payload) as response:
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue

                data = line.removeprefix("data: ")
                if data == "[DONE]":
                    return

                choice = json.loads(data)["choices"][0]
                yield BackendChunk(
                    content=choice["delta"].get("content", ""),
                    finish_reason=choice.get("finish_reason"),
                )

    async def _request_with_retry(self, request: ChatCompletionRequest) -> httpx.Response:
        attempts = self._max_retries + 1
        last_error: httpx.TransportError

        for _ in range(attempts):
            try:
                return await self._client.post(
                    "/v1/chat/completions",
                    json=_to_lmstudio_payload(request, stream=False),
                )
            except httpx.TransportError as exc:
                last_error = exc

        if isinstance(last_error, httpx.TimeoutException):
            logger.error("backend %s timed out after %d attempts", self.backend_id, attempts)
            raise BackendTimeoutError(str(last_error)) from last_error

        logger.error(
            "backend %s unreachable after %d attempts: %s", self.backend_id, attempts, last_error
        )
        raise BackendConnectionError(str(last_error)) from last_error


def _to_lmstudio_payload(request: ChatCompletionRequest, *, stream: bool) -> dict[str, object]:
    return request.model_dump(exclude={"stream"}) | {"stream": stream}

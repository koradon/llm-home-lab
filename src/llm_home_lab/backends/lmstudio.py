import json
import logging
from collections.abc import AsyncIterator

import httpx

from llm_home_lab.api.models import ChatCompletionRequest
from llm_home_lab.backends.base import (
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

    async def list_models(self) -> list[str] | None:
        # LM Studio's OpenAI-compatible /v1/models lists every downloaded model regardless of
        # load state (just-in-time loading would still trigger for any of them); the native
        # /api/v0/models endpoint reports a "loaded"/"not-loaded" state per model, which is the
        # only reliable signal for "won't trigger a new load."
        try:
            response = await self._client.get("/api/v0/models")
        except httpx.TransportError:
            return None

        if response.status_code // 100 != 2:
            return None

        return [entry["id"] for entry in response.json()["data"] if entry.get("state") == "loaded"]

    async def complete(self, request: ChatCompletionRequest) -> BackendResponse:
        # Talks to LM Studio via streaming even for a non-streaming caller: httpx's read timeout
        # resets on every received chunk, so this turns "max total generation time" into "max gap
        # between tokens" for every caller, not just ones that pass stream=True (ADR-0003).
        content_parts: list[str] = []
        finish_reason = "stop"
        usage: dict[str, int] | None = None

        async for chunk in self._stream_chunks(request):
            if chunk.content:
                content_parts.append(chunk.content)
            if chunk.finish_reason is not None:
                finish_reason = chunk.finish_reason
            if chunk.usage is not None:
                usage = chunk.usage

        if usage is None:
            logger.warning(
                "backend %s did not report usage for this completion; recording 0 tokens",
                self.backend_id,
            )
            usage = {"prompt_tokens": 0, "completion_tokens": 0}

        return BackendResponse(
            model=request.model,
            content="".join(content_parts),
            finish_reason=finish_reason,
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
        )

    def stream(self, request: ChatCompletionRequest) -> AsyncIterator[BackendChunk]:
        return self._stream_chunks(request)

    async def _stream_chunks(self, request: ChatCompletionRequest) -> AsyncIterator[BackendChunk]:
        payload = _to_lmstudio_payload(request)
        attempts = self._max_retries + 1
        last_error: httpx.TransportError | None = None

        for _ in range(attempts):
            received_any = False
            try:
                async with self._client.stream(
                    "POST", "/v1/chat/completions", json=payload
                ) as response:
                    if response.status_code // 100 != 2:
                        body = await response.aread()
                        raise BackendResponseError(
                            response.status_code, body.decode(errors="replace")
                        )

                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue

                        data = line.removeprefix("data: ")
                        if data == "[DONE]":
                            return

                        parsed = json.loads(data)
                        choices = parsed.get("choices") or []
                        chunk_usage = parsed.get("usage")

                        if choices:
                            choice = choices[0]
                            received_any = True
                            yield BackendChunk(
                                content=choice["delta"].get("content", ""),
                                finish_reason=choice.get("finish_reason"),
                                usage=chunk_usage,
                            )
                        elif chunk_usage is not None:
                            received_any = True
                            yield BackendChunk(content="", finish_reason=None, usage=chunk_usage)
                return
            except httpx.ReadTimeout as exc:
                # The request reached the backend; it's just still generating. Retrying would
                # resend the same prompt and wait the same timeout again, compounding the delay
                # instead of helping — fail immediately regardless of attempts remaining.
                logger.error("backend %s timed out waiting for a response", self.backend_id)
                raise BackendTimeoutError(str(exc)) from exc
            except httpx.TransportError as exc:
                if received_any:
                    # Some output already reached the caller (or, for complete(), was already
                    # accumulated) — restarting from scratch would duplicate or misrepresent it.
                    logger.error(
                        "backend %s connection failed mid-stream: %s", self.backend_id, exc
                    )
                    raise BackendConnectionError(str(exc)) from exc
                last_error = exc
                continue

        logger.error(
            "backend %s unreachable after %d attempts: %s", self.backend_id, attempts, last_error
        )
        raise BackendConnectionError(str(last_error)) from last_error


def _to_lmstudio_payload(request: ChatCompletionRequest) -> dict[str, object]:
    return request.model_dump(exclude={"stream"}) | {
        "stream": True,
        "stream_options": {"include_usage": True},
    }

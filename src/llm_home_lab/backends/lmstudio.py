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
from llm_home_lab.backends.http_stream import closing_chunks, inference_response

logger = logging.getLogger(__name__)


class LMStudioBackend:
    def __init__(
        self,
        base_url: str,
        timeout: float | None,
        max_retries: int = 2,
        connect_timeout: float = 10.0,
        health_timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        model_aliases: dict[str, list[str]] | None = None,
        pool_timeout: float = 10.0,
        write_timeout: float = 60.0,
    ) -> None:
        self.backend_id = base_url
        self.timeout = timeout
        self._max_retries = max_retries
        self.connect_timeout = connect_timeout
        self.health_timeout = health_timeout
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(
                timeout, connect=connect_timeout, pool=pool_timeout, write=write_timeout
            ),
            transport=transport,
        )
        self._model_aliases = model_aliases or {}
        self._alias_cursor: dict[str, int] = {}

    def _resolve_model(self, model: str) -> str:
        # Round-robins across the real LM Studio identifiers behind one client-facing model
        # name, so multiple loaded instances of the same model (each with its own full context)
        # can share one name without the caller needing to pick an instance itself.
        aliases = self._model_aliases.get(model)
        if not aliases:
            return model
        index = self._alias_cursor.get(model, 0) % len(aliases)
        self._alias_cursor[model] = index + 1
        return aliases[index]

    async def check_health(self) -> BackendHealth:
        # Control-plane liveness is bounded independently of inference progress.
        try:
            response = await self._client.get("/v1/models", timeout=self.health_timeout)
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
        # between received bytes" for every caller, not just ones that pass stream=True (ADR-0003).
        content_parts: list[str] = []
        finish_reason = "stop"
        usage: dict[str, int] | None = None

        async with closing_chunks(self._stream_chunks(request)) as chunks:
            async for chunk in chunks:
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
        # Resolved once per call, before the retry loop below, so a retry keeps hitting the
        # same instance it already started with instead of round-robining mid-request.
        payload = _to_lmstudio_payload(request, self._resolve_model(request.model))
        attempts = self._max_retries + 1
        last_error: httpx.TransportError | None = None

        for _ in range(attempts):
            received_any = False
            try:
                async with inference_response(self._client, payload) as response:
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
            except httpx.TimeoutException as exc:
                # Retrying a timeout can compound a stalled request. Keep transport
                # phase bounds independent of prefill/read silence and fail immediately.
                logger.error("backend %s request timed out: %s", self.backend_id, exc)
                raise BackendTimeoutError(str(exc) or "backend request timed out") from exc
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


def _to_lmstudio_payload(request: ChatCompletionRequest, model: str) -> dict[str, object]:
    # chat_template_kwargs is a llama.cpp extension, never forwarded to LM Studio (ADR-0012).
    excluded = {"stream", "chat_template_kwargs"} | request.unset_generation_fields()
    return request.model_dump(exclude=excluded) | {
        "model": model,
        "stream": True,
        "stream_options": {"include_usage": True},
    }

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
    RouterModelState,
)
from llm_home_lab.backends.http_stream import closing_chunks, inference_response
from llm_home_lab.registry.models import SwapMode

logger = logging.getLogger(__name__)

_SERVABLE_ROUTER_STATES = frozenset({"loaded", "sleeping"})


class LlamaCPPServerBackend:
    def __init__(
        self,
        base_url: str,
        timeout: float | None,
        max_retries: int = 2,
        connect_timeout: float = 10.0,
        health_timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        pool_timeout: float = 10.0,
        write_timeout: float = 60.0,
        swap_mode: SwapMode = "none",
    ) -> None:
        self.backend_id = base_url
        self.timeout = timeout
        self._max_retries = max_retries
        self.connect_timeout = connect_timeout
        self.health_timeout = health_timeout
        self.swap_mode: SwapMode = swap_mode
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(
                timeout, connect=connect_timeout, pool=pool_timeout, write=write_timeout
            ),
            transport=transport,
        )

    async def check_health(self) -> BackendHealth:
        # Control-plane liveness is bounded independently of inference progress.
        try:
            response = await self._client.get("/health", timeout=self.health_timeout)
        except httpx.TransportError as exc:
            return BackendHealth(healthy=False, detail=str(exc))

        if response.status_code // 100 != 2:
            return BackendHealth(
                healthy=False, detail=f"status {response.status_code}: {response.text}"
            )
        return BackendHealth(healthy=True, detail="ok")

    async def list_model_states(
        self, *, raise_on_timeout: bool = False
    ) -> list[RouterModelState] | None:
        # llama-server router mode exposes live catalog and load states on GET /models.
        try:
            response = await self._client.get("/models", timeout=self.health_timeout)
        except httpx.TimeoutException as exc:
            if raise_on_timeout:
                raise BackendTimeoutError("model inventory timed out") from exc
            return None
        except httpx.TransportError:
            return None

        if response.status_code // 100 != 2:
            return None

        try:
            payload = response.json()
        except ValueError:
            return None

        if not isinstance(payload, dict):
            return None
        data = payload.get("data")
        if not isinstance(data, list):
            return None

        states: list[RouterModelState] = []
        for entry in data:
            parsed = _parse_router_model_state(entry)
            if parsed is not None:
                states.append(parsed)
        return states

    async def list_models(self) -> list[str] | None:
        if self.swap_mode == "router":
            states = await self.list_model_states(raise_on_timeout=True)
            if states is None:
                return None
            return [
                entry.id
                for entry in states
                if not entry.failed and entry.state in _SERVABLE_ROUTER_STATES
            ]

        # A single-model llama-server process serves one model for its whole lifetime, so
        # unlike LM Studio there's no JIT-loading guard to enforce here — /v1/models always
        # reports that one model as loaded.
        try:
            response = await self._client.get("/v1/models")
        except httpx.TimeoutException as exc:
            raise BackendTimeoutError("model inventory timed out") from exc
        except httpx.TransportError:
            return None

        if response.status_code // 100 != 2:
            return None

        return [entry["id"] for entry in response.json()["data"]]

    async def complete(self, request: ChatCompletionRequest) -> BackendResponse:
        # Talks to llama-server via streaming even for a non-streaming caller: httpx's read
        # timeout resets on every received chunk, so this turns "max total generation time"
        # into "max gap between received bytes" for both response modes
        # (mirrors LMStudioBackend's ADR-0003 rationale).
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
        payload = _to_llamaserver_payload(request)
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


def _parse_router_model_state(entry: object) -> RouterModelState | None:
    if not isinstance(entry, dict):
        return None
    model_id = entry.get("id")
    if not isinstance(model_id, str) or not model_id:
        return None

    raw_status = entry.get("status")
    exit_code: int | None = None
    if isinstance(raw_status, dict):
        raw_value = raw_status.get("value")
        value = raw_value.lower() if isinstance(raw_value, str) and raw_value else "unloaded"
        failed = bool(raw_status.get("failed", False)) or value == "failed"
        raw_exit = raw_status.get("exit_code")
        if isinstance(raw_exit, int) and not isinstance(raw_exit, bool):
            exit_code = raw_exit
    elif isinstance(raw_status, str):
        value = raw_status.lower() or "unloaded"
        failed = value == "failed"
    else:
        value = "unloaded"
        failed = False

    state = "failed" if (failed and value not in _SERVABLE_ROUTER_STATES) else value
    return RouterModelState(id=model_id, state=state, failed=failed, exit_code=exit_code)


def _to_llamaserver_payload(request: ChatCompletionRequest) -> dict[str, object]:
    return request.model_dump(exclude={"stream"} | request.unset_generation_fields()) | {
        "stream": True,
        "stream_options": {"include_usage": True},
    }

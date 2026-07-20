import httpx
import pytest

from llm_home_lab.api.models import ChatCompletionRequest, Message
from llm_home_lab.backends.base import (
    BackendConnectionError,
    BackendResponseError,
    BackendTimeoutError,
)
from llm_home_lab.backends.lmstudio import LMStudioBackend


def _request():
    return ChatCompletionRequest(model="test-model", messages=[Message(role="user", content="Hi")])


def _sse_response(*lines: str) -> httpx.Response:
    body = "".join(f"data: {line}\n\n" for line in lines) + "data: [DONE]\n\n"
    return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})


SUCCESSFUL_SSE_LINES = (
    '{"choices": [{"delta": {"content": "Hello!"}, "finish_reason": null}]}',
    '{"choices": [{"delta": {}, "finish_reason": "stop"}]}',
    '{"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2}}',
)


async def test_successful_completion_returns_backend_response():
    def handler(request):
        return _sse_response(*SUCCESSFUL_SSE_LINES)

    transport = httpx.MockTransport(handler)
    backend = LMStudioBackend(
        base_url="http://lmstudio.local:1234", timeout=5.0, transport=transport
    )

    result = await backend.complete(_request())

    assert result.model == "test-model"
    assert result.content == "Hello!"
    assert result.finish_reason == "stop"
    assert result.prompt_tokens == 5
    assert result.completion_tokens == 2


async def test_completion_falls_back_to_zero_usage_when_backend_omits_it():
    def handler(request):
        return _sse_response('{"choices": [{"delta": {"content": "Hi"}, "finish_reason": "stop"}]}')

    transport = httpx.MockTransport(handler)
    backend = LMStudioBackend(
        base_url="http://lmstudio.local:1234", timeout=5.0, transport=transport
    )

    result = await backend.complete(_request())

    assert result.content == "Hi"
    assert result.prompt_tokens == 0
    assert result.completion_tokens == 0


async def test_non_2xx_response_raises_immediately_without_retry():
    call_count = 0

    def handler(request):
        nonlocal call_count
        call_count += 1
        return httpx.Response(500, json={"error": "internal error"})

    transport = httpx.MockTransport(handler)
    backend = LMStudioBackend(
        base_url="http://lmstudio.local:1234", timeout=5.0, transport=transport
    )

    with pytest.raises(BackendResponseError) as exc_info:
        await backend.complete(_request())

    assert exc_info.value.status_code == 500
    assert call_count == 1


async def test_read_timeout_raises_backend_timeout_error_immediately_without_retry():
    call_count = 0

    def handler(request):
        nonlocal call_count
        call_count += 1
        raise httpx.ReadTimeout("timed out", request=request)

    transport = httpx.MockTransport(handler)
    backend = LMStudioBackend(
        base_url="http://lmstudio.local:1234", timeout=5.0, max_retries=2, transport=transport
    )

    with pytest.raises(BackendTimeoutError):
        await backend.complete(_request())

    assert call_count == 1


async def test_connection_failure_exhausts_retries_and_raises_backend_connection_error():
    call_count = 0

    def handler(request):
        nonlocal call_count
        call_count += 1
        raise httpx.ConnectError("connection refused", request=request)

    transport = httpx.MockTransport(handler)
    backend = LMStudioBackend(
        base_url="http://lmstudio.local:1234", timeout=5.0, max_retries=2, transport=transport
    )

    with pytest.raises(BackendConnectionError):
        await backend.complete(_request())

    assert call_count == 3


async def test_transient_connection_failure_recovers_within_retry_budget():
    call_count = 0

    def handler(request):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.ConnectError("connection refused", request=request)
        return _sse_response(*SUCCESSFUL_SSE_LINES)

    transport = httpx.MockTransport(handler)
    backend = LMStudioBackend(
        base_url="http://lmstudio.local:1234", timeout=5.0, max_retries=2, transport=transport
    )

    result = await backend.complete(_request())

    assert result.content == "Hello!"
    assert call_count == 2


class _FlakyMidStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'data: {"choices": [{"delta": {"content": "Hi"}, "finish_reason": null}]}\n\n'
        raise httpx.ReadError("connection dropped mid-stream")


async def test_connection_failure_after_a_chunk_arrived_is_not_retried():
    call_count = 0

    def handler(request):
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            200, stream=_FlakyMidStream(), headers={"content-type": "text/event-stream"}
        )

    transport = httpx.MockTransport(handler)
    backend = LMStudioBackend(
        base_url="http://lmstudio.local:1234", timeout=5.0, max_retries=2, transport=transport
    )

    with pytest.raises(BackendConnectionError):
        async for _ in backend.stream(_request()):
            pass

    assert call_count == 1


async def test_stream_yields_backend_chunk_per_upstream_chunk():
    sse_body = (
        'data: {"choices": [{"delta": {"content": "Hel"}, "finish_reason": null}]}\n\n'
        'data: {"choices": [{"delta": {"content": "lo!"}, "finish_reason": null}]}\n\n'
        'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request):
        return httpx.Response(
            200, content=sse_body.encode(), headers={"content-type": "text/event-stream"}
        )

    transport = httpx.MockTransport(handler)
    backend = LMStudioBackend(
        base_url="http://lmstudio.local:1234", timeout=5.0, transport=transport
    )

    chunks = [chunk async for chunk in backend.stream(_request())]

    assert [c.content for c in chunks] == ["Hel", "lo!", ""]
    assert [c.finish_reason for c in chunks] == [None, None, "stop"]


async def test_classified_failure_is_logged_with_backend_host(caplog):
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    transport = httpx.MockTransport(handler)
    backend = LMStudioBackend(
        base_url="http://lmstudio.local:1234", timeout=5.0, max_retries=1, transport=transport
    )

    with caplog.at_level("ERROR"), pytest.raises(BackendTimeoutError):
        await backend.complete(_request())

    assert "http://lmstudio.local:1234" in caplog.text
    assert "timed out" in caplog.text.lower()


def test_backend_id_reflects_configured_host():
    backend = LMStudioBackend(base_url="http://lmstudio.local:1234", timeout=5.0)

    assert backend.backend_id == "http://lmstudio.local:1234"


def test_connect_timeout_defaults_to_10_seconds_independent_of_the_gap_timeout():
    backend = LMStudioBackend(base_url="http://lmstudio.local:1234", timeout=120.0)

    assert backend.connect_timeout == 10.0


def test_connect_timeout_is_configurable_independent_of_the_gap_timeout():
    backend = LMStudioBackend(
        base_url="http://lmstudio.local:1234", timeout=120.0, connect_timeout=3.0
    )

    assert backend.connect_timeout == 3.0


async def test_check_health_reports_healthy_when_models_endpoint_succeeds():
    def handler(request):
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": []})

    transport = httpx.MockTransport(handler)
    backend = LMStudioBackend(
        base_url="http://lmstudio.local:1234", timeout=5.0, transport=transport
    )

    health = await backend.check_health()

    assert health.healthy is True


async def test_list_models_returns_only_the_currently_loaded_model_ids():
    def handler(request):
        assert request.url.path == "/api/v0/models"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "qwen2.5-coder-14b-instruct-mlx", "state": "loaded"},
                    {"id": "google/gemma-4-e4b", "state": "loaded"},
                    {"id": "text-embedding-nomic-embed-text-v1.5", "state": "not-loaded"},
                ],
                "object": "list",
            },
        )

    transport = httpx.MockTransport(handler)
    backend = LMStudioBackend(
        base_url="http://lmstudio.local:1234", timeout=5.0, transport=transport
    )

    models = await backend.list_models()

    assert models == ["qwen2.5-coder-14b-instruct-mlx", "google/gemma-4-e4b"]


async def test_list_models_returns_none_on_a_non_2xx_response():
    def handler(request):
        return httpx.Response(500, text="internal error")

    transport = httpx.MockTransport(handler)
    backend = LMStudioBackend(
        base_url="http://lmstudio.local:1234", timeout=5.0, transport=transport
    )

    models = await backend.list_models()

    assert models is None


async def test_list_models_returns_none_when_backend_is_unreachable():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    transport = httpx.MockTransport(handler)
    backend = LMStudioBackend(
        base_url="http://lmstudio.local:1234", timeout=5.0, max_retries=0, transport=transport
    )

    models = await backend.list_models()

    assert models is None


async def test_check_health_reports_unhealthy_when_backend_is_unreachable():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    transport = httpx.MockTransport(handler)
    backend = LMStudioBackend(
        base_url="http://lmstudio.local:1234", timeout=5.0, max_retries=0, transport=transport
    )

    health = await backend.check_health()

    assert health.healthy is False
    assert "connection refused" in health.detail.lower()

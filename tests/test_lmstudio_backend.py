import httpx
import pytest

from orchestrator.api.models import ChatCompletionRequest, Message
from orchestrator.backends.base import (
    BackendConnectionError,
    BackendResponseError,
    BackendTimeoutError,
)
from orchestrator.backends.lmstudio import LMStudioBackend


def _request():
    return ChatCompletionRequest(model="test-model", messages=[Message(role="user", content="Hi")])


async def test_successful_completion_returns_backend_response():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "Hello!"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        )

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


async def test_timeout_exhausts_retries_and_raises_backend_timeout_error():
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

    assert call_count == 3


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


async def test_transient_failure_recovers_within_retry_budget():
    call_count = 0

    def handler(request):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "Hello!"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        )

    transport = httpx.MockTransport(handler)
    backend = LMStudioBackend(
        base_url="http://lmstudio.local:1234", timeout=5.0, max_retries=2, transport=transport
    )

    result = await backend.complete(_request())

    assert result.content == "Hello!"
    assert call_count == 2


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

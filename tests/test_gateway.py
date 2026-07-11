import json

from fastapi.testclient import TestClient

from llm_home_lab.api.app import create_app
from llm_home_lab.backends.base import BackendChunk, BackendResponse, BackendTimeoutError


class FakeBackend:
    async def complete(self, request):
        return BackendResponse(
            model=request.model,
            content="Hello!",
            finish_reason="stop",
            prompt_tokens=5,
            completion_tokens=2,
        )

    async def stream(self, request):
        yield BackendChunk(content="Hel", finish_reason=None)
        yield BackendChunk(content="lo!", finish_reason=None)
        yield BackendChunk(content="", finish_reason="stop")


def test_valid_non_streaming_request_returns_openai_shaped_response():
    client = TestClient(create_app(backend=FakeBackend()))
    payload = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "Hi"}],
        "stream": False,
    }

    response = client.post("/v1/chat/completions", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == "test-model"
    assert body["choices"][0]["message"] == {"role": "assistant", "content": "Hello!"}
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"] == {
        "prompt_tokens": 5,
        "completion_tokens": 2,
        "total_tokens": 7,
    }
    assert "id" in body
    assert "created" in body


def test_missing_messages_field_is_rejected_with_error_envelope():
    client = TestClient(create_app(backend=FakeBackend()))
    payload = {"model": "test-model", "stream": False}

    response = client.post("/v1/chat/completions", json=payload)

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["type"] == "invalid_request_error"
    assert "message" in body["error"]
    assert "code" in body["error"]


def test_empty_messages_array_is_rejected():
    client = TestClient(create_app(backend=FakeBackend()))
    payload = {"model": "test-model", "messages": [], "stream": False}

    response = client.post("/v1/chat/completions", json=payload)

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


def test_unrecognized_top_level_field_is_tolerated():
    client = TestClient(create_app(backend=FakeBackend()))
    payload = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "Hi"}],
        "top_p": 0.9,
    }

    response = client.post("/v1/chat/completions", json=payload)

    assert response.status_code == 200


def test_backend_timeout_is_surfaced_as_gateway_error():
    class TimingOutBackend:
        async def complete(self, request):
            raise BackendTimeoutError("backend did not respond in time")

    client = TestClient(create_app(backend=TimingOutBackend()))
    payload = {"model": "test-model", "messages": [{"role": "user", "content": "Hi"}]}

    response = client.post("/v1/chat/completions", json=payload)

    assert response.status_code == 504
    body = response.json()
    assert body["error"]["type"] == "backend_error"
    assert "message" in body["error"]


def test_malformed_json_body_is_rejected():
    client = TestClient(create_app(backend=FakeBackend()))

    response = client.post(
        "/v1/chat/completions",
        content="{not valid json",
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


def test_streaming_request_returns_sse_chunks_ending_in_done():
    client = TestClient(create_app(backend=FakeBackend()))
    payload = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "Hi"}],
        "stream": True,
    }

    response = client.post("/v1/chat/completions", json=payload)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    lines = [line for line in response.text.split("\n\n") if line]
    assert lines[-1] == "data: [DONE]"
    first_chunk = json.loads(lines[0].removeprefix("data: "))
    assert first_chunk["object"] == "chat.completion.chunk"
    assert first_chunk["choices"][0]["delta"] == {"role": "assistant", "content": "Hel"}
    last_data_chunk = json.loads(lines[-2].removeprefix("data: "))
    assert last_data_chunk["choices"][0]["finish_reason"] == "stop"

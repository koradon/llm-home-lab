from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.backends.base import BackendHealth, BackendResponse


class FakeBackend:
    backend_id = "fake-backend"

    def __init__(self, healthy: bool = True, detail: str = "ok"):
        self._healthy = healthy
        self._detail = detail

    async def complete(self, request):
        return BackendResponse(
            model=request.model,
            content="Hello!",
            finish_reason="stop",
            prompt_tokens=5,
            completion_tokens=2,
        )

    async def stream(self, request):
        return
        yield

    async def check_health(self):
        return BackendHealth(healthy=self._healthy, detail=self._detail)


def test_liveness_always_returns_ok():
    client = TestClient(create_app(backend=FakeBackend()))

    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_reports_healthy_backend():
    client = TestClient(create_app(backend=FakeBackend(healthy=True)))

    response = client.get("/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["backends"] == [{"id": "fake-backend", "healthy": True, "detail": "ok"}]


def test_readiness_reports_unhealthy_backend():
    client = TestClient(create_app(backend=FakeBackend(healthy=False, detail="connection refused")))

    response = client.get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["backends"] == [
        {"id": "fake-backend", "healthy": False, "detail": "connection refused"}
    ]


def test_every_request_produces_one_structured_log_line(caplog):
    client = TestClient(create_app(backend=FakeBackend()))

    with caplog.at_level("INFO"):
        response = client.get("/health/live")

    assert response.status_code == 200
    lines = [record.message for record in caplog.records if record.name == "orchestrator.access"]
    assert len(lines) == 1
    assert "request_id=" in lines[0]
    assert "method=GET" in lines[0]
    assert "status=200" in lines[0]
    assert "latency_ms=" in lines[0]


def test_inbound_request_id_is_reused(caplog):
    client = TestClient(create_app(backend=FakeBackend()))

    with caplog.at_level("INFO"):
        response = client.get("/health/live", headers={"X-Request-ID": "req-abc-123"})

    assert response.headers["X-Request-ID"] == "req-abc-123"
    access_lines = [
        record.message for record in caplog.records if record.name == "orchestrator.access"
    ]
    assert "request_id=req-abc-123" in access_lines[0]


def test_request_id_is_generated_when_absent():
    client = TestClient(create_app(backend=FakeBackend()))

    response = client.get("/health/live")

    assert response.headers["X-Request-ID"]
    assert len(response.headers["X-Request-ID"]) > 0

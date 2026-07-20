import httpx
import pytest

from llm_home_lab.tui.client import DiagnosticsClientError, OrchestratorDiagnosticsClient


def _client(handler) -> OrchestratorDiagnosticsClient:
    transport = httpx.MockTransport(handler)
    return OrchestratorDiagnosticsClient(
        base_url="http://orchestrator.local:8080", api_key="test-key", transport=transport
    )


async def test_list_nodes_returns_parsed_json_on_success():
    def handler(request):
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(200, json={"nodes": [{"host_id": "host-a"}]})

    client = _client(handler)

    result = await client.list_nodes()

    assert result == {"nodes": [{"host_id": "host-a"}]}


async def test_list_alerts_returns_parsed_json_on_success():
    def handler(request):
        assert request.url.path == "/v1/alerts"
        return httpx.Response(200, json={"alerts": [{"rule_name": "latency"}]})

    client = _client(handler)

    result = await client.list_alerts()

    assert result == {"alerts": [{"rule_name": "latency"}]}


async def test_fetch_metrics_text_returns_raw_body_on_success():
    def handler(request):
        assert request.url.path == "/metrics"
        return httpx.Response(200, text="llm_home_lab_queue_depth 2\n")

    client = _client(handler)

    result = await client.fetch_metrics_text()

    assert result == "llm_home_lab_queue_depth 2\n"


async def test_unauthorized_response_raises_unauthorized_kind():
    def handler(request):
        return httpx.Response(401, json={"error": "unauthorized"})

    client = _client(handler)

    with pytest.raises(DiagnosticsClientError) as exc_info:
        await client.list_nodes()
    assert exc_info.value.kind == "unauthorized"


async def test_server_error_response_raises_server_error_kind():
    def handler(request):
        return httpx.Response(500, json={"error": "boom"})

    client = _client(handler)

    with pytest.raises(DiagnosticsClientError) as exc_info:
        await client.list_nodes()
    assert exc_info.value.kind == "server_error"


async def test_connection_failure_raises_connection_kind():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    client = _client(handler)

    with pytest.raises(DiagnosticsClientError) as exc_info:
        await client.list_nodes()
    assert exc_info.value.kind == "connection"


async def test_trigger_health_check_calls_health_ready():
    def handler(request):
        assert request.url.path == "/health/ready"
        return httpx.Response(200, json={"status": "ok", "backends": []})

    client = _client(handler)

    await client.trigger_health_check()


async def test_trigger_health_check_does_not_raise_on_503():
    def handler(request):
        return httpx.Response(503, json={"status": "unavailable", "backends": []})

    client = _client(handler)

    await client.trigger_health_check()


async def test_trigger_health_check_raises_connection_kind_on_transport_failure():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    client = _client(handler)

    with pytest.raises(DiagnosticsClientError) as exc_info:
        await client.trigger_health_check()
    assert exc_info.value.kind == "connection"

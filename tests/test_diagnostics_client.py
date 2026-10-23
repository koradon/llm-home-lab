import json

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


async def test_update_node_sends_a_patch_request_with_the_given_fields():
    def handler(request):
        assert request.method == "PATCH"
        assert request.url.path == "/v1/nodes/host-a"
        assert json.loads(request.content) == {"max_concurrent_requests": 8}
        return httpx.Response(200, json={"status": "updated"})

    client = _client(handler)

    await client.update_node("host-a", {"max_concurrent_requests": 8})


async def test_update_node_encodes_a_host_id_containing_slashes_into_the_path():
    def handler(request):
        assert request.url.path == "/v1/nodes/http://localhost:1234"
        return httpx.Response(200, json={"status": "updated"})

    client = _client(handler)

    await client.update_node("http://localhost:1234", {"max_concurrent_requests": 8})


async def test_update_node_raises_unauthorized_kind_on_401():
    def handler(request):
        return httpx.Response(401, json={"error": "unauthorized"})

    client = _client(handler)

    with pytest.raises(DiagnosticsClientError) as exc_info:
        await client.update_node("host-a", {"max_concurrent_requests": 8})
    assert exc_info.value.kind == "unauthorized"


async def test_update_node_raises_connection_kind_on_transport_failure():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    client = _client(handler)

    with pytest.raises(DiagnosticsClientError) as exc_info:
        await client.update_node("host-a", {"max_concurrent_requests": 8})
    assert exc_info.value.kind == "connection"


async def test_register_node_sends_a_post_request_with_the_given_fields():
    def handler(request):
        assert request.method == "POST"
        assert request.url.path == "/v1/nodes/register"
        assert json.loads(request.content) == {"host_id": "host-a", "base_url": "http://x:1"}
        return httpx.Response(200, json={"status": "registered"})

    client = _client(handler)

    await client.register_node({"host_id": "host-a", "base_url": "http://x:1"})


async def test_register_node_raises_server_error_kind_on_5xx():
    def handler(request):
        return httpx.Response(500, json={"error": "boom"})

    client = _client(handler)

    with pytest.raises(DiagnosticsClientError) as exc_info:
        await client.register_node({"host_id": "host-a"})
    assert exc_info.value.kind == "server_error"


async def test_register_node_raises_connection_kind_on_transport_failure():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    client = _client(handler)

    with pytest.raises(DiagnosticsClientError) as exc_info:
        await client.register_node({"host_id": "host-a"})
    assert exc_info.value.kind == "connection"


async def test_deregister_node_sends_a_delete_request_to_the_hosts_path():
    def handler(request):
        assert request.method == "DELETE"
        assert request.url.path == "/v1/nodes/host-a"
        return httpx.Response(200, json={"status": "deregistered"})

    client = _client(handler)

    await client.deregister_node("host-a")


async def test_deregister_node_raises_unauthorized_kind_on_401():
    def handler(request):
        return httpx.Response(401, json={"error": "unauthorized"})

    client = _client(handler)

    with pytest.raises(DiagnosticsClientError) as exc_info:
        await client.deregister_node("host-a")
    assert exc_info.value.kind == "unauthorized"


async def test_deregister_node_raises_connection_kind_on_transport_failure():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    client = _client(handler)

    with pytest.raises(DiagnosticsClientError) as exc_info:
        await client.deregister_node("host-a")
    assert exc_info.value.kind == "connection"


async def test_a_request_timeout_raises_timeout_kind_not_connection():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    client = _client(handler)

    with pytest.raises(DiagnosticsClientError) as exc_info:
        await client.list_nodes()
    assert exc_info.value.kind == "timeout"


async def test_a_refused_connection_still_raises_connection_kind():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    client = _client(handler)

    with pytest.raises(DiagnosticsClientError) as exc_info:
        await client.list_nodes()
    assert exc_info.value.kind == "connection"


@pytest.mark.parametrize(
    "operation,args",
    [
        ("list_nodes", ()),
        ("list_alerts", ()),
        ("fetch_metrics_text", ()),
        ("register_node", ({"host_id": "host-a"},)),
        ("update_node", ("host-a", {"max_concurrent_requests": 8})),
        ("deregister_node", ("host-a",)),
    ],
)
@pytest.mark.parametrize(
    "error,kind", [(httpx.ConnectTimeout, "connection"), (httpx.ReadTimeout, "timeout")]
)
async def test_timeout_classification_across_operations(operation, args, error, kind):
    def handler(request):
        raise error("timed out", request=request)

    client = _client(handler)

    with pytest.raises(DiagnosticsClientError) as exc_info:
        await getattr(client, operation)(*args)

    assert exc_info.value.kind == kind

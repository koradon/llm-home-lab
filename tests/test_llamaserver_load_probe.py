from datetime import UTC, datetime, timedelta

import httpx

from llm_home_lab.registry.llamaserver_load import LlamaCPPServerLoadProbe

T0 = datetime(2026, 1, 1, tzinfo=UTC)


async def test_a_busy_slot_reports_busy_status_and_queued_count():
    def handler(request):
        assert request.url.path == "/slots"
        return httpx.Response(
            200,
            json=[
                {"id": 0, "n_ctx": 65536, "is_processing": True, "speculative": False},
                {"id": 1, "n_ctx": 65536, "is_processing": False, "speculative": False},
            ],
        )

    transport = httpx.MockTransport(handler)
    probe = LlamaCPPServerLoadProbe(transport=transport)

    result = await probe.probe("host-a", "http://192.168.1.10:8080", at=T0)

    assert result.available is True
    assert result.status == "busy"
    assert result.queued == 1


async def test_no_busy_slots_reports_idle():
    def handler(request):
        return httpx.Response(
            200,
            json=[{"id": 0, "n_ctx": 65536, "is_processing": False, "speculative": False}],
        )

    transport = httpx.MockTransport(handler)
    probe = LlamaCPPServerLoadProbe(transport=transport)

    result = await probe.probe("host-a", "http://192.168.1.10:8080", at=T0)

    assert result.available is True
    assert result.status == "idle"
    assert result.queued == 0


async def test_no_slots_reports_idle_not_unavailable():
    def handler(request):
        return httpx.Response(200, json=[])

    transport = httpx.MockTransport(handler)
    probe = LlamaCPPServerLoadProbe(transport=transport)

    result = await probe.probe("host-a", "http://192.168.1.10:8080", at=T0)

    assert result.available is True
    assert result.status == "idle"
    assert result.queued == 0


async def test_disabled_slots_endpoint_reports_unavailable_not_unhealthy():
    def handler(request):
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    probe = LlamaCPPServerLoadProbe(transport=transport)

    result = await probe.probe("host-a", "http://192.168.1.10:8080", at=T0)

    assert result.available is False
    assert result.status is None
    assert result.queued is None


async def test_non_2xx_response_reports_unavailable():
    def handler(request):
        return httpx.Response(500, text="internal error")

    transport = httpx.MockTransport(handler)
    probe = LlamaCPPServerLoadProbe(transport=transport)

    result = await probe.probe("host-a", "http://192.168.1.10:8080", at=T0)

    assert result.available is False


async def test_unparseable_output_reports_unavailable():
    def handler(request):
        return httpx.Response(200, content=b"not json")

    transport = httpx.MockTransport(handler)
    probe = LlamaCPPServerLoadProbe(transport=transport)

    result = await probe.probe("host-a", "http://192.168.1.10:8080", at=T0)

    assert result.available is False


async def test_unreachable_host_reports_unavailable():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    transport = httpx.MockTransport(handler)
    probe = LlamaCPPServerLoadProbe(transport=transport)

    result = await probe.probe("host-a", "http://192.168.1.10:8080", at=T0)

    assert result.available is False


async def test_a_cached_result_is_reused_within_the_ttl():
    call_count = 0

    def handler(request):
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json=[{"id": 0, "is_processing": False}])

    transport = httpx.MockTransport(handler)
    probe = LlamaCPPServerLoadProbe(cache_ttl=timedelta(seconds=10), transport=transport)

    await probe.probe("host-a", "http://192.168.1.10:8080", at=T0)
    await probe.probe("host-a", "http://192.168.1.10:8080", at=T0 + timedelta(seconds=5))

    assert call_count == 1


async def test_a_stale_cached_result_triggers_a_fresh_probe():
    call_count = 0

    def handler(request):
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json=[{"id": 0, "is_processing": False}])

    transport = httpx.MockTransport(handler)
    probe = LlamaCPPServerLoadProbe(cache_ttl=timedelta(seconds=10), transport=transport)

    await probe.probe("host-a", "http://192.168.1.10:8080", at=T0)
    await probe.probe("host-a", "http://192.168.1.10:8080", at=T0 + timedelta(seconds=11))

    assert call_count == 2


async def test_two_hosts_are_probed_independently():
    def handler(request):
        if "192.168.1.10" in str(request.url):
            return httpx.Response(404)
        return httpx.Response(200, json=[{"id": 0, "is_processing": False}])

    transport = httpx.MockTransport(handler)
    probe = LlamaCPPServerLoadProbe(transport=transport)

    result_a = await probe.probe("host-a", "http://192.168.1.10:8080", at=T0)
    result_b = await probe.probe("host-b", "http://192.168.1.20:8080", at=T0)

    assert result_a.available is False
    assert result_b.available is True

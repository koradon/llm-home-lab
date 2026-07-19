import asyncio
from datetime import UTC, datetime

import httpx

from llm_home_lab.api.app import create_app
from llm_home_lab.backends.base import BackendResponse
from llm_home_lab.health.monitor import HealthMonitor
from llm_home_lab.observability.alerts import AlertEvaluator
from llm_home_lab.observability.metrics import MetricsRegistry
from llm_home_lab.registry.models import HostCapabilities, HostCapacity
from llm_home_lab.registry.registry import HostRegistry
from llm_home_lab.routing.engine import RoutingEngine
from llm_home_lab.routing.models import PolicyRule, RoutingPolicy
from llm_home_lab.scheduling.queue import SchedulingQueue
from llm_home_lab.security.key_store import ApiKeyStore
from llm_home_lab.security.models import ApiKey, ClientConfig

PAYLOAD = {"model": "test-model", "messages": [{"role": "user", "content": "hi"}]}
AUTH_HEADERS = {"Authorization": "Bearer test-key"}


def _permissive_key_store() -> ApiKeyStore:
    return ApiKeyStore(
        [
            ClientConfig(
                client_id="test-client",
                allowed_path_prefixes=["/"],
                keys=[ApiKey(key="test-key", expires_at=None)],
            )
        ]
    )


class SlowBackend:
    backend_id = "slow-backend"

    def __init__(self) -> None:
        self.started_event = asyncio.Event()
        self.release_event = asyncio.Event()

    async def complete(self, request):
        self.started_event.set()
        await self.release_event.wait()
        return BackendResponse(
            model=request.model,
            content="done",
            finish_reason="stop",
            prompt_tokens=1,
            completion_tokens=1,
        )


def _app(backend, max_concurrent_requests=1, dispatch_wait_timeout=30.0):
    registry = HostRegistry()
    registry.register(
        backend.backend_id,
        HostCapabilities(backend_type="slow", context_window=8192, base_url="unused"),
        HostCapacity(max_concurrent_requests=max_concurrent_requests),
        at=datetime.now(UTC),
    )
    policy = RoutingPolicy(rules=[PolicyRule(name="flat", score_fn=lambda c, ctx: 0.0)])
    return create_app(
        registry=registry,
        router=RoutingEngine(policy),
        health_monitor=HealthMonitor(),
        scheduling_queue=SchedulingQueue(),
        backend_factories={"slow": lambda caps: backend},
        metrics_registry=MetricsRegistry(),
        alert_evaluator=AlertEvaluator([]),
        key_store=_permissive_key_store(),
        dispatch_wait_timeout=dispatch_wait_timeout,
        dispatch_poll_interval=0.01,
    )


async def test_a_second_request_queues_until_the_first_releases_its_slot():
    backend = SlowBackend()
    transport = httpx.ASGITransport(app=_app(backend))

    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=AUTH_HEADERS
    ) as client:
        first = asyncio.create_task(client.post("/v1/chat/completions", json=PAYLOAD))
        await backend.started_event.wait()

        second = asyncio.create_task(client.post("/v1/chat/completions", json=PAYLOAD))
        await asyncio.sleep(0.05)
        assert not second.done()

        backend.release_event.set()
        first_response, second_response = await first, await second

    assert first_response.status_code == 200
    assert second_response.status_code == 200


async def test_a_request_that_never_gets_a_free_slot_times_out_as_service_unavailable():
    backend = SlowBackend()
    transport = httpx.ASGITransport(app=_app(backend, dispatch_wait_timeout=0.05))

    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=AUTH_HEADERS
    ) as client:
        first = asyncio.create_task(client.post("/v1/chat/completions", json=PAYLOAD))
        await backend.started_event.wait()

        response = await client.post("/v1/chat/completions", json=PAYLOAD)

    assert response.status_code == 503
    assert response.json()["error"]["type"] == "backend_error"
    backend.release_event.set()
    await first

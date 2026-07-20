import asyncio
from datetime import UTC, datetime

import httpx
from registry_test_helpers import inert_external_load_probe, new_registry_db_path

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
    def __init__(self, backend_id: str) -> None:
        self.backend_id = backend_id
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


async def test_second_request_prefers_the_less_loaded_host():
    backend_a = SlowBackend("host-a")
    backend_b = SlowBackend("host-b")
    registry = HostRegistry(new_registry_db_path())
    for backend in (backend_a, backend_b):
        registry.register(
            backend.backend_id,
            HostCapabilities(backend_type="slow", context_window=8192, base_url=backend.backend_id),
            HostCapacity(max_concurrent_requests=2),
            at=datetime.now(UTC),
        )
    policy = RoutingPolicy(
        rules=[PolicyRule(name="prefer-lower-latency", score_fn=lambda c, ctx: -c.latency_ms)]
    )
    backends_by_base_url = {backend_a.backend_id: backend_a, backend_b.backend_id: backend_b}
    app = create_app(
        registry=registry,
        router=RoutingEngine(policy),
        health_monitor=HealthMonitor(),
        scheduling_queue=SchedulingQueue(),
        backend_factories={"slow": lambda caps: backends_by_base_url[caps.base_url]},
        metrics_registry=MetricsRegistry(),
        alert_evaluator=AlertEvaluator([]),
        key_store=_permissive_key_store(),
        external_load_probe=inert_external_load_probe(),
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=AUTH_HEADERS
    ) as client:
        first = asyncio.create_task(client.post("/v1/chat/completions", json=PAYLOAD))
        await backend_a.started_event.wait()

        second = asyncio.create_task(client.post("/v1/chat/completions", json=PAYLOAD))
        await asyncio.wait_for(backend_b.started_event.wait(), timeout=1.0)

        backend_a.release_event.set()
        backend_b.release_event.set()
        first_response, second_response = await asyncio.gather(first, second)

    assert first_response.status_code == 200
    assert second_response.status_code == 200

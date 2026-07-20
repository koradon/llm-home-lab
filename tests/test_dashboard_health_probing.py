from datetime import UTC, datetime

import httpx
from registry_test_helpers import inert_external_load_probe, new_registry_db_path
from textual.widgets import DataTable

from llm_home_lab.api.app import create_app
from llm_home_lab.backends.base import BackendHealth
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
from llm_home_lab.tui.app import DashboardApp
from llm_home_lab.tui.client import OrchestratorDiagnosticsClient


class HealthyBackend:
    backend_id = "host-a"

    async def check_health(self):
        return BackendHealth(healthy=True, detail="ok")


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


def _orchestrator_app():
    backend = HealthyBackend()
    registry = HostRegistry(new_registry_db_path())
    registry.register(
        backend.backend_id,
        HostCapabilities(backend_type="fake", context_window=8192, base_url="unused"),
        HostCapacity(max_concurrent_requests=4),
        at=datetime.now(UTC),
    )
    policy = RoutingPolicy(rules=[PolicyRule(name="flat", score_fn=lambda c, ctx: 0.0)])
    return create_app(
        registry=registry,
        router=RoutingEngine(policy),
        health_monitor=HealthMonitor(),
        scheduling_queue=SchedulingQueue(),
        backend_factories={"fake": lambda caps, b=backend: b},
        metrics_registry=MetricsRegistry(),
        alert_evaluator=AlertEvaluator([]),
        key_store=_permissive_key_store(),
        external_load_probe=inert_external_load_probe(),
    )


async def test_dashboard_poll_triggers_a_health_probe_so_status_leaves_unknown():
    transport = httpx.ASGITransport(app=_orchestrator_app())
    client = OrchestratorDiagnosticsClient(
        base_url="http://test", api_key="test-key", transport=transport
    )
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test():
        await app.poll()

        table = app.query_one("#nodes-table", DataTable)
        row_key, _ = table.coordinate_to_cell_key((0, 0))
        column_key = table.ordered_columns[1].key
        status_cell = table.get_cell(row_key, column_key)
        assert str(status_cell) == "online"

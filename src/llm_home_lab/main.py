import logging
import os
from datetime import UTC, datetime

from fastapi import FastAPI

from llm_home_lab.api.app import create_app
from llm_home_lab.backends.lmstudio import LMStudioBackend
from llm_home_lab.health.monitor import HealthMonitor
from llm_home_lab.registry.models import HostCapabilities, HostCapacity
from llm_home_lab.registry.registry import HostRegistry
from llm_home_lab.routing.engine import RoutingEngine
from llm_home_lab.routing.models import PolicyRule, RoutingPolicy
from llm_home_lab.scheduling.queue import SchedulingQueue
from llm_home_lab.security.key_store import ApiKeyStore

DEFAULT_API_KEYS_FILE = "./config/api_keys.json"

logger = logging.getLogger(__name__)


def _auth_enabled() -> bool:
    return os.environ.get("ORCHESTRATOR_AUTH_ENABLED", "true").strip().lower() not in (
        "false",
        "0",
        "no",
    )


def _load_key_store() -> ApiKeyStore:
    path = os.environ.get("ORCHESTRATOR_API_KEYS_FILE", DEFAULT_API_KEYS_FILE)
    if not os.path.exists(path):
        # No clients configured yet: every non-exempt request is unauthorized until an
        # operator creates this file (see docs/security-baseline.md), rather than failing
        # to start.
        return ApiKeyStore([])
    return ApiKeyStore.from_file(path)


BACKEND_FACTORIES = {
    "lmstudio": lambda caps: LMStudioBackend(
        base_url=caps.base_url,
        timeout=float(os.environ.get("LMSTUDIO_TIMEOUT", "30")),
        max_retries=int(os.environ.get("LMSTUDIO_MAX_RETRIES", "2")),
    ),
}


def create_default_app() -> FastAPI:
    registry = HostRegistry()
    base_url = os.environ.get("LMSTUDIO_BASE_URL", "http://localhost:1234")
    registry.register(
        base_url,
        HostCapabilities(
            backend_type="lmstudio",
            context_window=int(os.environ.get("LMSTUDIO_CONTEXT_WINDOW", "8192")),
            base_url=base_url,
        ),
        HostCapacity(
            max_concurrent_requests=int(os.environ.get("LMSTUDIO_MAX_CONCURRENT_REQUESTS", "4"))
        ),
        at=datetime.now(UTC),
    )
    policy = RoutingPolicy(
        rules=[PolicyRule(name="prefer-lower-latency", score_fn=lambda c, ctx: -c.latency_ms)]
    )
    router = RoutingEngine(policy)
    health_monitor = HealthMonitor()
    auth_enabled = _auth_enabled()
    if not auth_enabled:
        logger.warning(
            "ORCHESTRATOR_AUTH_ENABLED=false: authentication is disabled, every request is "
            "admitted unchecked. Do not use this in production."
        )
    return create_app(
        registry=registry,
        router=router,
        health_monitor=health_monitor,
        scheduling_queue=SchedulingQueue(),
        backend_factories=BACKEND_FACTORIES,
        key_store=_load_key_store() if auth_enabled else None,
        auth_enabled=auth_enabled,
    )


app = create_default_app()


def run() -> None:
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("ORCHESTRATOR_HOST", "0.0.0.0"),
        port=int(os.environ.get("ORCHESTRATOR_PORT", "8080")),
    )


if __name__ == "__main__":
    run()

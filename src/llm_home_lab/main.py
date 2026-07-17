import os

from fastapi import FastAPI

from llm_home_lab.api.app import create_app
from llm_home_lab.backends.lmstudio import LMStudioBackend
from llm_home_lab.health.monitor import HealthMonitor
from llm_home_lab.routing.engine import RoutingEngine
from llm_home_lab.routing.models import PolicyRule, RoutingCandidate, RoutingPolicy


def create_default_app() -> FastAPI:
    backend = LMStudioBackend(
        base_url=os.environ.get("LMSTUDIO_BASE_URL", "http://localhost:1234"),
        timeout=float(os.environ.get("LMSTUDIO_TIMEOUT", "30")),
        max_retries=int(os.environ.get("LMSTUDIO_MAX_RETRIES", "2")),
    )
    candidates = [
        RoutingCandidate(
            backend=backend,
            latency_ms=0.0,
            context_window=int(os.environ.get("LMSTUDIO_CONTEXT_WINDOW", "8192")),
        )
    ]
    policy = RoutingPolicy(
        rules=[PolicyRule(name="prefer-lower-latency", score_fn=lambda c, ctx: -c.latency_ms)]
    )
    router = RoutingEngine(policy)
    health_monitor = HealthMonitor()
    return create_app(candidates=candidates, router=router, health_monitor=health_monitor)


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

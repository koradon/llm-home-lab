import os

from fastapi import FastAPI

from orchestrator.api.app import create_app
from orchestrator.backends.lmstudio import LMStudioBackend


def create_default_app() -> FastAPI:
    backend = LMStudioBackend(
        base_url=os.environ.get("LMSTUDIO_BASE_URL", "http://localhost:1234"),
        timeout=float(os.environ.get("LMSTUDIO_TIMEOUT", "30")),
        max_retries=int(os.environ.get("LMSTUDIO_MAX_RETRIES", "2")),
    )
    return create_app(backend=backend)


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

from llm_home_lab.backends.lmstudio import LMStudioBackend
from llm_home_lab.main import create_default_app


def test_default_app_wires_lmstudio_backend_with_default_config(monkeypatch):
    monkeypatch.delenv("LMSTUDIO_BASE_URL", raising=False)
    monkeypatch.delenv("LMSTUDIO_TIMEOUT", raising=False)
    monkeypatch.delenv("LMSTUDIO_MAX_RETRIES", raising=False)

    app = create_default_app()

    backend = app.state.backend
    assert isinstance(backend, LMStudioBackend)
    assert backend.backend_id == "http://localhost:1234"


def test_default_app_respects_lmstudio_env_overrides(monkeypatch):
    monkeypatch.setenv("LMSTUDIO_BASE_URL", "http://gpu-box.home:1234")
    monkeypatch.setenv("LMSTUDIO_TIMEOUT", "45")
    monkeypatch.setenv("LMSTUDIO_MAX_RETRIES", "5")

    app = create_default_app()

    backend = app.state.backend
    assert backend.backend_id == "http://gpu-box.home:1234"


def test_default_app_exposes_gateway_and_health_routes():
    app = create_default_app()

    paths = {route.path for route in app.routes}

    assert "/v1/chat/completions" in paths
    assert "/health/live" in paths
    assert "/health/ready" in paths

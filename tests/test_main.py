from llm_home_lab.main import create_default_app


def test_default_app_wires_lmstudio_backend_with_default_config(monkeypatch):
    monkeypatch.delenv("LMSTUDIO_BASE_URL", raising=False)
    monkeypatch.delenv("LMSTUDIO_TIMEOUT", raising=False)
    monkeypatch.delenv("LMSTUDIO_MAX_RETRIES", raising=False)

    app = create_default_app()

    hosts = app.state.registry.hosts()
    assert [host.host_id for host in hosts] == ["http://localhost:1234"]
    assert hosts[0].capabilities.backend_type == "lmstudio"


def test_default_app_respects_lmstudio_env_overrides(monkeypatch):
    monkeypatch.setenv("LMSTUDIO_BASE_URL", "http://gpu-box.home:1234")
    monkeypatch.setenv("LMSTUDIO_TIMEOUT", "45")
    monkeypatch.setenv("LMSTUDIO_MAX_RETRIES", "5")

    app = create_default_app()

    hosts = app.state.registry.hosts()
    assert [host.host_id for host in hosts] == ["http://gpu-box.home:1234"]


def test_default_app_has_auth_enabled_by_default(monkeypatch):
    monkeypatch.delenv("ORCHESTRATOR_AUTH_ENABLED", raising=False)

    app = create_default_app()

    assert app.state.auth_enabled is True


def test_default_app_disables_auth_when_env_var_is_false(monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_AUTH_ENABLED", "false")

    app = create_default_app()

    assert app.state.auth_enabled is False


def test_default_app_exposes_gateway_and_health_routes():
    app = create_default_app()

    paths = {route.path for route in app.routes}

    assert "/v1/chat/completions" in paths
    assert "/health/live" in paths
    assert "/health/ready" in paths

from llm_home_lab.main import _load_alert_evaluator, create_default_app


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


def test_default_app_uses_a_120_second_dispatch_wait_timeout_by_default(monkeypatch):
    monkeypatch.delenv("ORCHESTRATOR_DISPATCH_WAIT_TIMEOUT_S", raising=False)

    app = create_default_app()

    assert app.state.dispatch_wait_timeout == 120.0


def test_default_app_respects_dispatch_wait_timeout_env_override(monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_DISPATCH_WAIT_TIMEOUT_S", "90")

    app = create_default_app()

    assert app.state.dispatch_wait_timeout == 90.0


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


def test_default_app_exposes_metrics_and_alerts_routes(monkeypatch):
    monkeypatch.delenv("ORCHESTRATOR_ALERT_RULES_FILE", raising=False)

    app = create_default_app()

    paths = {route.path for route in app.routes}

    assert "/metrics" in paths
    assert "/v1/alerts" in paths


def test_the_committed_default_alert_rules_file_parses_without_error(monkeypatch):
    monkeypatch.delenv("ORCHESTRATOR_ALERT_RULES_FILE", raising=False)

    evaluator = _load_alert_evaluator()

    assert evaluator.current_state() == []


def test_a_missing_alert_rules_file_yields_an_empty_evaluator_not_a_crash(monkeypatch, tmp_path):
    monkeypatch.setenv("ORCHESTRATOR_ALERT_RULES_FILE", str(tmp_path / "does-not-exist.json"))

    evaluator = _load_alert_evaluator()

    assert evaluator.current_state() == []

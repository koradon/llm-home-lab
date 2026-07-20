import pytest

from llm_home_lab.main import BACKEND_FACTORIES, _load_alert_evaluator, create_default_app
from llm_home_lab.registry.models import HostCapabilities


@pytest.fixture(autouse=True)
def _isolated_host_registry_db(monkeypatch, tmp_path):
    monkeypatch.setenv("ORCHESTRATOR_HOST_REGISTRY_DB_PATH", str(tmp_path / "host_registry.db"))


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


def test_lmstudio_backend_factory_uses_a_10_second_connect_timeout_by_default(monkeypatch):
    monkeypatch.delenv("LMSTUDIO_CONNECT_TIMEOUT", raising=False)
    caps = HostCapabilities(backend_type="lmstudio", context_window=8192, base_url="http://x:1234")

    backend = BACKEND_FACTORIES["lmstudio"](caps)

    assert backend.connect_timeout == 10.0


def test_lmstudio_backend_factory_respects_connect_timeout_env_override(monkeypatch):
    monkeypatch.setenv("LMSTUDIO_CONNECT_TIMEOUT", "3")
    caps = HostCapabilities(backend_type="lmstudio", context_window=8192, base_url="http://x:1234")

    backend = BACKEND_FACTORIES["lmstudio"](caps)

    assert backend.connect_timeout == 3.0


def test_default_app_uses_lms_binary_by_default(monkeypatch):
    monkeypatch.delenv("ORCHESTRATOR_LMS_BINARY_PATH", raising=False)

    app = create_default_app()

    assert app.state.external_load_probe.lms_binary == "lms"


def test_default_app_respects_lms_binary_path_env_override(monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_LMS_BINARY_PATH", "/opt/lms/lms")

    app = create_default_app()

    assert app.state.external_load_probe.lms_binary == "/opt/lms/lms"


def test_default_app_uses_a_2_second_external_load_probe_interval_by_default(monkeypatch):
    monkeypatch.delenv("ORCHESTRATOR_EXTERNAL_LOAD_PROBE_INTERVAL_S", raising=False)

    app = create_default_app()

    assert app.state.external_load_probe.cache_ttl.total_seconds() == 2.0


def test_default_app_respects_external_load_probe_interval_env_override(monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_EXTERNAL_LOAD_PROBE_INTERVAL_S", "30")

    app = create_default_app()

    assert app.state.external_load_probe.cache_ttl.total_seconds() == 30.0


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

from datetime import UTC, datetime

from registry_test_helpers import new_registry_db_path

from llm_home_lab.registry.models import HostCapabilities, HostCapacity
from llm_home_lab.registry.multi_backend_load_probe import MultiBackendLoadProbe
from llm_home_lab.registry.registry import HostRegistry

T0 = datetime(2026, 1, 1, tzinfo=UTC)


class _FakeProbe:
    def __init__(self, status):
        self._status = status
        self.calls = []

    async def probe(self, host_id, base_url, at):
        self.calls.append((host_id, base_url, at))
        return self._status


def _registry_with_host(host_id: str, backend_type: str) -> HostRegistry:
    registry = HostRegistry(new_registry_db_path())
    registry.register(
        host_id,
        HostCapabilities(backend_type=backend_type, context_window=8192, base_url=host_id),
        HostCapacity(max_concurrent_requests=1),
        at=T0,
    )
    return registry


async def test_dispatches_to_the_probe_registered_for_the_hosts_backend_type():
    registry = _registry_with_host("http://llama.local:8080", backend_type="llamaserver")
    llamaserver_probe = _FakeProbe("llamaserver-status")
    lmstudio_probe = _FakeProbe("lmstudio-status")
    dispatcher = MultiBackendLoadProbe(
        registry=registry,
        probes_by_backend_type={"llamaserver": llamaserver_probe, "lmstudio": lmstudio_probe},
    )

    result = await dispatcher.probe("http://llama.local:8080", "http://llama.local:8080", at=T0)

    assert result == "llamaserver-status"
    assert lmstudio_probe.calls == []


async def test_a_host_with_no_registered_probe_for_its_backend_type_reports_unavailable():
    registry = _registry_with_host("http://unknown.local:9999", backend_type="mystery")
    dispatcher = MultiBackendLoadProbe(registry=registry, probes_by_backend_type={})

    result = await dispatcher.probe("http://unknown.local:9999", "http://unknown.local:9999", at=T0)

    assert result.available is False
    assert result.status is None
    assert result.queued is None


async def test_a_deregistered_host_reports_unavailable_instead_of_raising():
    registry = HostRegistry(new_registry_db_path())
    dispatcher = MultiBackendLoadProbe(registry=registry, probes_by_backend_type={})

    result = await dispatcher.probe("http://gone.local:8080", "http://gone.local:8080", at=T0)

    assert result.available is False


def test_probe_for_returns_the_registered_probe_by_backend_type():
    registry = HostRegistry(new_registry_db_path())
    llamaserver_probe = _FakeProbe("status")
    dispatcher = MultiBackendLoadProbe(
        registry=registry, probes_by_backend_type={"llamaserver": llamaserver_probe}
    )

    assert dispatcher.probe_for("llamaserver") is llamaserver_probe
    assert dispatcher.probe_for("lmstudio") is None

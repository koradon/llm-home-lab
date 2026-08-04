from collections.abc import Mapping
from datetime import datetime

from llm_home_lab.registry.external_load import ExternalLoadStatus, LoadProbe
from llm_home_lab.registry.models import HostNotRegisteredError
from llm_home_lab.registry.registry import HostRegistry


def _unavailable(at: datetime) -> ExternalLoadStatus:
    return ExternalLoadStatus(available=False, status=None, queued=None, checked_at=at)


class MultiBackendLoadProbe:
    # create_app's external_load_probe hook only ever calls probe(host_id, base_url, at) — not
    # backend_type — so picking the right prober per host has to happen here, via a registry
    # lookup, rather than at that call site (kept that way to avoid touching its many existing
    # callers across the test suite).
    def __init__(
        self, registry: HostRegistry, probes_by_backend_type: Mapping[str, LoadProbe]
    ) -> None:
        self._registry = registry
        self._probes_by_backend_type = probes_by_backend_type

    def probe_for(self, backend_type: str) -> LoadProbe | None:
        return self._probes_by_backend_type.get(backend_type)

    async def probe(self, host_id: str, base_url: str, at: datetime) -> ExternalLoadStatus:
        try:
            host = self._registry.get(host_id)
        except HostNotRegisteredError:
            return _unavailable(at)

        probe = self._probes_by_backend_type.get(host.capabilities.backend_type)
        if probe is None:
            return _unavailable(at)

        return await probe.probe(host_id, base_url, at)

from collections.abc import Sequence
from datetime import datetime

from llm_home_lab.registry.models import (
    HostCapabilities,
    HostCapacity,
    HostInfo,
    HostNotRegisteredError,
)
from llm_home_lab.registry.store import HostRegistryStore


class HostRegistry:
    def __init__(self, db_path: str) -> None:
        self._store = HostRegistryStore(db_path)
        self._hosts: dict[str, HostInfo] = {host.host_id: host for host in self._store.load_hosts()}

    def register(
        self,
        host_id: str,
        capabilities: HostCapabilities,
        capacity: HostCapacity,
        at: datetime,
    ) -> None:
        existing = self._hosts.get(host_id)
        self._hosts[host_id] = HostInfo(
            host_id=host_id,
            capabilities=capabilities,
            capacity=capacity,
            in_flight=existing.in_flight if existing is not None else 0,
            last_seen=at,
            registration_id=existing.registration_id if existing is not None else object(),
            in_flight_by_model=(dict(existing.in_flight_by_model) if existing is not None else {}),
        )
        self._store.upsert(host_id, capabilities, capacity, at)

    def get(self, host_id: str) -> HostInfo:
        if host_id not in self._hosts:
            raise HostNotRegisteredError(host_id)
        return self._hosts[host_id]

    def hosts(self) -> Sequence[HostInfo]:
        return list(self._hosts.values())

    def in_flight(self, host_id: str) -> int:
        return self._hosts[host_id].in_flight

    def acquire_slot(self, host_id: str) -> None:
        self._hosts[host_id].in_flight += 1

    def acquire_slot_for_model(self, host_id: str, model: str | None = None) -> None:
        self.acquire_slot(host_id)
        if model:
            host = self._hosts[host_id]
            host.in_flight_by_model[model] = host.in_flight_by_model.get(model, 0) + 1

    def release_slot(self, host_id: str) -> None:
        self._hosts[host_id].in_flight -= 1

    def release_owned_slot(
        self, host_id: str, registration_id: object, model: str | None = None
    ) -> None:
        host = self._hosts.get(host_id)
        if host is not None and host.registration_id is registration_id:
            self.release_slot(host_id)
            if model:
                remaining = host.in_flight_by_model.get(model, 0) - 1
                if remaining > 0:
                    host.in_flight_by_model[model] = remaining
                else:
                    host.in_flight_by_model.pop(model, None)

    def heartbeat(self, host_id: str, at: datetime) -> None:
        if host_id not in self._hosts:
            raise HostNotRegisteredError(host_id)
        self._hosts[host_id].last_seen = at
        self._store.update_last_seen(host_id, at)

    def deregister(self, host_id: str) -> None:
        self._hosts.pop(host_id, None)
        self._store.delete(host_id)

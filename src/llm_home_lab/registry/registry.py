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
        )
        self._store.upsert(host_id, capabilities, capacity, at)

    def hosts(self) -> Sequence[HostInfo]:
        return list(self._hosts.values())

    def in_flight(self, host_id: str) -> int:
        return self._hosts[host_id].in_flight

    def acquire_slot(self, host_id: str) -> None:
        self._hosts[host_id].in_flight += 1

    def release_slot(self, host_id: str) -> None:
        self._hosts[host_id].in_flight -= 1

    def heartbeat(self, host_id: str, at: datetime) -> None:
        if host_id not in self._hosts:
            raise HostNotRegisteredError(host_id)
        self._hosts[host_id].last_seen = at
        self._store.update_last_seen(host_id, at)

    def deregister(self, host_id: str) -> None:
        self._hosts.pop(host_id, None)
        self._store.delete(host_id)

import secrets
import threading
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from typing import NoReturn

from llm_home_lab.leases.models import (
    LeaseEndedError,
    LeaseInvalidError,
    LeaseNotFoundError,
    LeaseNotHeldError,
    LeaseRefusedError,
    LeaseSettings,
    NodeLease,
    ReservableHost,
)
from llm_home_lab.registry.models import HostNotRegisteredError


class NodeLeaseBook:
    """In-memory node leases.

    A restart drops every lease, which returns those nodes to routing immediately.
    Callers pass ``now`` so tests can move time without sleeping.
    """

    def __init__(self, settings: LeaseSettings | None = None) -> None:
        self._settings = settings or LeaseSettings()
        self._lock = threading.Lock()
        self._by_host: dict[str, NodeLease] = {}
        self._by_token: dict[str, NodeLease] = {}

    @property
    def settings(self) -> LeaseSettings:
        return self._settings

    def reserve(
        self,
        *,
        holder: str,
        hosts: Sequence[ReservableHost],
        host_id: str | None,
        model: str | None,
        ceiling: timedelta | None,
        now: datetime,
    ) -> NodeLease:
        cleaned = holder.strip()
        if not cleaned:
            raise LeaseInvalidError("holder must not be empty")
        if ceiling is None:
            ceiling = self._settings.default_ceiling
        if ceiling <= timedelta(0):
            raise LeaseInvalidError("ceiling must be positive")
        if ceiling > self._settings.max_ceiling:
            raise LeaseInvalidError(
                f"ceiling of {int(ceiling.total_seconds())}s exceeds the server maximum "
                f"of {int(self._settings.max_ceiling.total_seconds())}s"
            )

        with self._lock:
            self._purge_unlocked(now)
            known = {host.host_id: host for host in hosts}
            if host_id is not None:
                host = known.get(host_id)
                if host is None:
                    raise HostNotRegisteredError(host_id)
                return self._reserve_specific(host, hosts, cleaned, model, ceiling, now)
            return self._reserve_any(hosts, cleaned, model, ceiling, now)

    def require(self, token: str, now: datetime) -> NodeLease:
        with self._lock:
            lease = self._by_token.get(token)
            if lease is None:
                raise LeaseNotHeldError("lease token is not valid")
            if now >= lease.expires_at or now >= lease.hard_expires_at:
                self._drop_unlocked(lease)
                raise LeaseEndedError(f"lease on {lease.host_id} held by {lease.holder} has ended")
            return lease

    def is_held(self, host_id: str, now: datetime) -> bool:
        with self._lock:
            lease = self._by_host.get(host_id)
            if lease is None:
                return False
            if now >= lease.expires_at or now >= lease.hard_expires_at:
                self._drop_unlocked(lease)
                return False
            return True

    def heartbeat(self, token: str, now: datetime) -> NodeLease:
        with self._lock:
            lease = self._live_unlocked(token, now)
            updated = replace(
                lease, expires_at=min(now + self._settings.ttl, lease.hard_expires_at)
            )
            self._by_host[updated.host_id] = updated
            self._by_token[updated.token] = updated
            return updated

    def release(self, token: str, now: datetime) -> NodeLease:
        with self._lock:
            lease = self._live_unlocked(token, now)
            self._drop_unlocked(lease)
            return lease

    def force_release(self, host_id: str, now: datetime) -> NodeLease:
        with self._lock:
            self._purge_unlocked(now)
            lease = self._by_host.get(host_id)
            if lease is None:
                raise LeaseNotFoundError(f"{host_id} has no lease")
            self._drop_unlocked(lease)
            return lease

    def view(self, host_id: str, now: datetime) -> dict[str, object] | None:
        with self._lock:
            lease = self._by_host.get(host_id)
            if lease is None:
                return None
            if now >= lease.expires_at or now >= lease.hard_expires_at:
                self._drop_unlocked(lease)
                return None
            remaining = min(lease.expires_at, lease.hard_expires_at) - now
            return lease.public_dict(
                include_token=False,
                expiring_soon=remaining <= self._settings.expiring_soon,
            )

    def _reserve_specific(
        self,
        host: ReservableHost,
        hosts: Sequence[ReservableHost],
        holder: str,
        model: str | None,
        ceiling: timedelta,
        now: datetime,
    ) -> NodeLease:
        if model is not None and not host.serves_requested_model:
            self._refuse(hosts, f"{host.host_id} does not serve model {model!r}")
        existing = self._by_host.get(host.host_id)
        if existing is not None:
            self._refuse(
                hosts,
                f"{host.host_id} is already reserved by {existing.holder} "
                f"until {existing.expires_at.isoformat()}",
            )
        if host.healthy and self._active_count(hosts) - 1 < self._settings.min_active_nodes:
            remaining = self._active_count(hosts) - 1
            self._refuse(
                hosts,
                f"reserving {host.host_id} would leave {remaining} active "
                f"node(s) in routing (minimum {self._settings.min_active_nodes})",
            )
        return self._grant(host.host_id, holder, ceiling, now)

    def _reserve_any(
        self,
        hosts: Sequence[ReservableHost],
        holder: str,
        model: str | None,
        ceiling: timedelta,
        now: datetime,
    ) -> NodeLease:
        matching = [host for host in hosts if model is None or host.serves_requested_model]
        if not matching:
            reason = (
                f"no node serves model {model!r}" if model is not None else "no node is registered"
            )
            self._refuse(hosts, reason)
        free = [host for host in matching if host.host_id not in self._by_host]
        if not free:
            self._refuse(hosts, "no node is free")
        active_free = sorted((host for host in free if host.healthy), key=lambda host: host.host_id)
        spare = self._active_count(hosts) - self._settings.min_active_nodes
        if spare <= 0 or not active_free:
            self._refuse(
                hosts,
                "no node can be reserved without leaving routing below the minimum of "
                f"{self._settings.min_active_nodes} active node(s)",
            )
        return self._grant(active_free[0].host_id, holder, ceiling, now)

    def _grant(self, host_id: str, holder: str, ceiling: timedelta, now: datetime) -> NodeLease:
        hard_expires_at = now + ceiling
        lease = NodeLease(
            host_id=host_id,
            holder=holder,
            token=secrets.token_urlsafe(32),
            expires_at=min(now + self._settings.ttl, hard_expires_at),
            hard_expires_at=hard_expires_at,
            created_at=now,
        )
        self._by_host[host_id] = lease
        self._by_token[lease.token] = lease
        return lease

    def _active_count(self, hosts: Sequence[ReservableHost]) -> int:
        return sum(1 for host in hosts if host.healthy and host.host_id not in self._by_host)

    def _live_unlocked(self, token: str, now: datetime) -> NodeLease:
        lease = self._by_token.get(token)
        if lease is None:
            raise LeaseNotHeldError("lease token is not valid")
        if now >= lease.expires_at or now >= lease.hard_expires_at:
            self._drop_unlocked(lease)
            raise LeaseEndedError(f"lease on {lease.host_id} held by {lease.holder} has ended")
        return lease

    def _purge_unlocked(self, now: datetime) -> None:
        expired = [
            lease
            for lease in self._by_host.values()
            if now >= lease.expires_at or now >= lease.hard_expires_at
        ]
        for lease in expired:
            self._drop_unlocked(lease)

    def _drop_unlocked(self, lease: NodeLease) -> None:
        self._by_host.pop(lease.host_id, None)
        self._by_token.pop(lease.token, None)

    def _refuse(self, hosts: Sequence[ReservableHost], reason: str) -> NoReturn:
        raise LeaseRefusedError(f"{reason}\n{self._roster(hosts)}")

    def _roster(self, hosts: Sequence[ReservableHost]) -> str:
        if not hosts:
            return "nodes: (none registered)"
        lines = ["nodes:"]
        for host in hosts:
            lease = self._by_host.get(host.host_id)
            if lease is None:
                lines.append(f"- {host.host_id} status={host.status} holder=free")
                continue
            lines.append(
                f"- {host.host_id} status={host.status} holder={lease.holder} "
                f"until {lease.expires_at.isoformat()} "
                f"ceiling {lease.hard_expires_at.isoformat()}"
            )
        return "\n".join(lines)

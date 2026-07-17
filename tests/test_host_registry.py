from datetime import UTC, datetime, timedelta

import pytest

from llm_home_lab.registry.models import HostCapabilities, HostCapacity, HostNotRegisteredError
from llm_home_lab.registry.registry import HostRegistry

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _capabilities(
    backend_type: str = "lmstudio",
    context_window: int = 8192,
    base_url: str = "http://localhost:1234",
) -> HostCapabilities:
    return HostCapabilities(
        backend_type=backend_type, context_window=context_window, base_url=base_url
    )


def _capacity(max_concurrent_requests: int = 2) -> HostCapacity:
    return HostCapacity(max_concurrent_requests=max_concurrent_requests)


def test_registering_a_host_makes_it_appear_in_the_host_list():
    registry = HostRegistry()

    registry.register("host-a", _capabilities(), _capacity(), at=T0)

    assert [host.host_id for host in registry.hosts()] == ["host-a"]


def test_acquiring_a_slot_increments_in_flight_count():
    registry = HostRegistry()
    registry.register("host-a", _capabilities(), _capacity(), at=T0)

    registry.acquire_slot("host-a")

    assert registry.in_flight("host-a") == 1


def test_releasing_a_slot_decrements_in_flight_count():
    registry = HostRegistry()
    registry.register("host-a", _capabilities(), _capacity(), at=T0)
    registry.acquire_slot("host-a")

    registry.release_slot("host-a")

    assert registry.in_flight("host-a") == 0


def test_reregistering_updates_capacity_without_resetting_in_flight_count():
    registry = HostRegistry()
    registry.register("host-a", _capabilities(), _capacity(max_concurrent_requests=2), at=T0)
    registry.acquire_slot("host-a")

    registry.register(
        "host-a",
        _capabilities(),
        _capacity(max_concurrent_requests=5),
        at=T0 + timedelta(seconds=1),
    )

    assert registry.in_flight("host-a") == 1
    assert registry.hosts()[0].capacity.max_concurrent_requests == 5


def test_heartbeat_updates_last_seen():
    registry = HostRegistry()
    registry.register("host-a", _capabilities(), _capacity(), at=T0)

    registry.heartbeat("host-a", at=T0 + timedelta(seconds=5))

    assert registry.hosts()[0].last_seen == T0 + timedelta(seconds=5)


def test_heartbeat_on_an_unregistered_host_raises():
    registry = HostRegistry()

    with pytest.raises(HostNotRegisteredError):
        registry.heartbeat("host-a", at=T0)


def test_deregister_removes_a_host_immediately():
    registry = HostRegistry()
    registry.register("host-a", _capabilities(), _capacity(), at=T0)

    registry.deregister("host-a")

    assert registry.hosts() == []


def test_expire_stale_removes_hosts_past_the_ttl_but_keeps_fresh_ones():
    registry = HostRegistry()
    registry.register("stale-host", _capabilities(), _capacity(), at=T0)
    registry.register("fresh-host", _capabilities(), _capacity(), at=T0 + timedelta(seconds=50))

    registry.expire_stale(at=T0 + timedelta(seconds=60), ttl=timedelta(seconds=30))

    assert [host.host_id for host in registry.hosts()] == ["fresh-host"]

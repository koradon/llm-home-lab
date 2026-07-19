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


def test_registering_a_host_makes_it_appear_in_the_host_list(tmp_path):
    registry = HostRegistry(str(tmp_path / "registry.db"))

    registry.register("host-a", _capabilities(), _capacity(), at=T0)

    assert [host.host_id for host in registry.hosts()] == ["host-a"]


def test_acquiring_a_slot_increments_in_flight_count(tmp_path):
    registry = HostRegistry(str(tmp_path / "registry.db"))
    registry.register("host-a", _capabilities(), _capacity(), at=T0)

    registry.acquire_slot("host-a")

    assert registry.in_flight("host-a") == 1


def test_releasing_a_slot_decrements_in_flight_count(tmp_path):
    registry = HostRegistry(str(tmp_path / "registry.db"))
    registry.register("host-a", _capabilities(), _capacity(), at=T0)
    registry.acquire_slot("host-a")

    registry.release_slot("host-a")

    assert registry.in_flight("host-a") == 0


def test_reregistering_updates_capacity_without_resetting_in_flight_count(tmp_path):
    registry = HostRegistry(str(tmp_path / "registry.db"))
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


def test_heartbeat_updates_last_seen(tmp_path):
    registry = HostRegistry(str(tmp_path / "registry.db"))
    registry.register("host-a", _capabilities(), _capacity(), at=T0)

    registry.heartbeat("host-a", at=T0 + timedelta(seconds=5))

    assert registry.hosts()[0].last_seen == T0 + timedelta(seconds=5)


def test_heartbeat_on_an_unregistered_host_raises(tmp_path):
    registry = HostRegistry(str(tmp_path / "registry.db"))

    with pytest.raises(HostNotRegisteredError):
        registry.heartbeat("host-a", at=T0)


def test_deregister_removes_a_host_immediately(tmp_path):
    registry = HostRegistry(str(tmp_path / "registry.db"))
    registry.register("host-a", _capabilities(), _capacity(), at=T0)

    registry.deregister("host-a")

    assert registry.hosts() == []


def test_a_registered_host_survives_a_fresh_registry_instance_against_the_same_db(tmp_path):
    db_path = str(tmp_path / "registry.db")
    original = HostRegistry(db_path)
    original.register("host-a", _capabilities(base_url="http://host-a:1234"), _capacity(5), at=T0)

    reloaded = HostRegistry(db_path)

    hosts = reloaded.hosts()
    assert [host.host_id for host in hosts] == ["host-a"]
    assert hosts[0].capabilities.base_url == "http://host-a:1234"
    assert hosts[0].capacity.max_concurrent_requests == 5
    assert hosts[0].last_seen == T0


def test_a_deregistered_host_stays_gone_for_a_fresh_registry_instance(tmp_path):
    db_path = str(tmp_path / "registry.db")
    original = HostRegistry(db_path)
    original.register("host-a", _capabilities(), _capacity(), at=T0)
    original.deregister("host-a")

    reloaded = HostRegistry(db_path)

    assert reloaded.hosts() == []

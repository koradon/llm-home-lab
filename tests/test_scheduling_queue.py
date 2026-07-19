from datetime import UTC, datetime

from registry_test_helpers import new_registry_db_path

from llm_home_lab.registry.models import HostCapabilities, HostCapacity
from llm_home_lab.registry.registry import HostRegistry
from llm_home_lab.scheduling.queue import SchedulingQueue

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _registry_with_host(host_id: str = "host-a", max_concurrent_requests: int = 1) -> HostRegistry:
    registry = HostRegistry(new_registry_db_path())
    registry.register(
        host_id,
        HostCapabilities(
            backend_type="lmstudio", context_window=8192, base_url="http://localhost:1234"
        ),
        HostCapacity(max_concurrent_requests=max_concurrent_requests),
        at=T0,
    )
    return registry


def test_dispatch_admits_a_queued_request_when_a_host_has_capacity():
    queue = SchedulingQueue()
    registry = _registry_with_host()
    queue.enqueue("req-1", session_id="session-1", priority=0, at=T0)

    dispatched = queue.dispatch(registry, at=T0)

    assert dispatched == "req-1"


def test_dispatch_returns_none_when_no_host_has_capacity():
    queue = SchedulingQueue()
    registry = _registry_with_host(max_concurrent_requests=1)
    registry.acquire_slot("host-a")
    queue.enqueue("req-1", session_id="session-1", priority=0, at=T0)

    dispatched = queue.dispatch(registry, at=T0)

    assert dispatched is None


def test_lower_priority_number_dispatches_before_a_higher_one_regardless_of_enqueue_order():
    queue = SchedulingQueue()
    registry = _registry_with_host()
    queue.enqueue("low-priority-req", session_id="session-1", priority=5, at=T0)
    queue.enqueue("high-priority-req", session_id="session-2", priority=0, at=T0)

    dispatched = queue.dispatch(registry, at=T0)

    assert dispatched == "high-priority-req"


def test_fairness_alternates_between_sessions_at_the_same_priority():
    queue = SchedulingQueue()
    registry = _registry_with_host()
    queue.enqueue("session-1-req-a", session_id="session-1", priority=0, at=T0)
    queue.enqueue("session-1-req-b", session_id="session-1", priority=0, at=T0)
    queue.enqueue("session-1-req-c", session_id="session-1", priority=0, at=T0)
    queue.enqueue("session-2-req-a", session_id="session-2", priority=0, at=T0)

    dispatched_order = [queue.dispatch(registry, at=T0) for _ in range(4)]

    assert dispatched_order == [
        "session-1-req-a",
        "session-2-req-a",
        "session-1-req-b",
        "session-1-req-c",
    ]


def test_dispatch_returns_none_when_the_queue_has_never_had_anything_enqueued():
    queue = SchedulingQueue()
    registry = _registry_with_host()

    dispatched = queue.dispatch(registry, at=T0)

    assert dispatched is None


def test_dispatch_skips_a_fully_drained_tier_to_reach_a_lower_priority_tier():
    queue = SchedulingQueue()
    registry = _registry_with_host()
    queue.enqueue("high-priority-req", session_id="session-1", priority=0, at=T0)
    assert queue.dispatch(registry, at=T0) == "high-priority-req"

    queue.enqueue("low-priority-req", session_id="session-2", priority=5, at=T0)
    dispatched = queue.dispatch(registry, at=T0)

    assert dispatched == "low-priority-req"


def test_depth_is_zero_for_an_empty_queue():
    queue = SchedulingQueue()

    assert queue.depth() == 0


def test_depth_counts_entries_across_tiers_and_sessions_and_drops_on_dispatch():
    queue = SchedulingQueue()
    registry = _registry_with_host()
    queue.enqueue("req-a", session_id="session-1", priority=0, at=T0)
    queue.enqueue("req-b", session_id="session-2", priority=0, at=T0)
    queue.enqueue("req-c", session_id="session-1", priority=5, at=T0)

    assert queue.depth() == 3

    queue.dispatch(registry, at=T0)

    assert queue.depth() == 2


def test_draining_one_session_fully_then_enqueueing_a_new_session_dispatches_the_new_session():
    queue = SchedulingQueue()
    registry = _registry_with_host()
    queue.enqueue("session-1-req-a", session_id="session-1", priority=0, at=T0)
    assert queue.dispatch(registry, at=T0) == "session-1-req-a"

    queue.enqueue("session-2-req-a", session_id="session-2", priority=0, at=T0)
    dispatched = queue.dispatch(registry, at=T0)

    assert dispatched == "session-2-req-a"


def test_cancel_removes_an_abandoned_entry_so_it_no_longer_counts_toward_depth():
    queue = SchedulingQueue()
    queue.enqueue("req-a", session_id="session-1", priority=0, at=T0)

    queue.cancel("req-a", session_id="session-1", priority=0)

    assert queue.depth() == 0


def test_cancel_of_one_entry_leaves_other_queued_entries_dispatchable():
    queue = SchedulingQueue()
    registry = _registry_with_host()
    queue.enqueue("req-a", session_id="session-1", priority=0, at=T0)
    queue.enqueue("req-b", session_id="session-2", priority=0, at=T0)

    queue.cancel("req-a", session_id="session-1", priority=0)

    assert queue.depth() == 1
    assert queue.dispatch(registry, at=T0) == "req-b"


def test_cancel_of_one_of_two_entries_in_the_same_session_keeps_the_other():
    queue = SchedulingQueue()
    registry = _registry_with_host()
    queue.enqueue("req-a", session_id="session-1", priority=0, at=T0)
    queue.enqueue("req-b", session_id="session-1", priority=0, at=T0)

    queue.cancel("req-a", session_id="session-1", priority=0)

    assert queue.depth() == 1
    assert queue.dispatch(registry, at=T0) == "req-b"


def test_cancel_of_an_unknown_request_id_is_a_no_op():
    queue = SchedulingQueue()
    queue.enqueue("req-a", session_id="session-1", priority=0, at=T0)

    queue.cancel("does-not-exist", session_id="session-1", priority=0)

    assert queue.depth() == 1

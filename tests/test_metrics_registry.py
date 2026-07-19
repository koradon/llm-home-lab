from datetime import UTC, datetime, timedelta

from registry_test_helpers import new_registry_db_path

from llm_home_lab.observability.metrics import MetricsRegistry
from llm_home_lab.registry.models import HostCapabilities, HostCapacity
from llm_home_lab.registry.registry import HostRegistry
from llm_home_lab.scheduling.queue import SchedulingQueue

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def test_availability_is_1_when_no_requests_have_been_recorded():
    metrics = MetricsRegistry()

    snapshot = metrics.snapshot(T0, HostRegistry(new_registry_db_path()), SchedulingQueue())

    assert snapshot.availability == 1.0


def test_availability_reflects_the_ratio_of_non_5xx_responses():
    metrics = MetricsRegistry()
    metrics.record_request("/v1/chat/completions", 200, 10.0, T0)
    metrics.record_request("/v1/chat/completions", 200, 10.0, T0)
    metrics.record_request("/v1/chat/completions", 500, 10.0, T0)
    metrics.record_request("/v1/chat/completions", 400, 10.0, T0)

    snapshot = metrics.snapshot(T0, HostRegistry(new_registry_db_path()), SchedulingQueue())

    assert snapshot.availability == 0.75


def test_requests_older_than_the_window_no_longer_affect_availability():
    metrics = MetricsRegistry(window=timedelta(minutes=5))
    metrics.record_request("/v1/chat/completions", 500, 10.0, T0)
    later = T0 + timedelta(minutes=6)
    metrics.record_request("/v1/chat/completions", 200, 10.0, later)

    snapshot = metrics.snapshot(later, HostRegistry(new_registry_db_path()), SchedulingQueue())

    assert snapshot.availability == 1.0


def test_p95_latency_is_0_when_no_requests_have_been_recorded():
    metrics = MetricsRegistry()

    snapshot = metrics.snapshot(T0, HostRegistry(new_registry_db_path()), SchedulingQueue())

    assert snapshot.p95_latency_ms == 0.0


def test_p95_latency_reflects_recorded_request_latencies():
    metrics = MetricsRegistry()
    for latency_ms in [10.0, 20.0, 30.0, 40.0, 100.0]:
        metrics.record_request("/v1/chat/completions", 200, latency_ms, T0)

    snapshot = metrics.snapshot(T0, HostRegistry(new_registry_db_path()), SchedulingQueue())

    assert snapshot.p95_latency_ms == 100.0


def test_failover_success_rate_is_none_when_no_failover_was_ever_involved():
    metrics = MetricsRegistry()

    snapshot = metrics.snapshot(T0, HostRegistry(new_registry_db_path()), SchedulingQueue())

    assert snapshot.failover_success_rate is None


def test_failover_success_rate_reflects_recorded_outcomes():
    metrics = MetricsRegistry()
    metrics.record_failover_outcome(True, T0)
    metrics.record_failover_outcome(True, T0)
    metrics.record_failover_outcome(False, T0)

    snapshot = metrics.snapshot(T0, HostRegistry(new_registry_db_path()), SchedulingQueue())

    assert snapshot.failover_success_rate == 2 / 3


def test_token_usage_accumulates_per_host_and_is_not_windowed():
    metrics = MetricsRegistry(window=timedelta(minutes=5))
    metrics.record_token_usage("host-a", prompt_tokens=10, completion_tokens=5, at=T0)
    much_later = T0 + timedelta(hours=1)
    metrics.record_token_usage("host-a", prompt_tokens=20, completion_tokens=5, at=much_later)
    metrics.record_token_usage("host-b", prompt_tokens=1, completion_tokens=1, at=much_later)

    snapshot = metrics.snapshot(much_later, HostRegistry(new_registry_db_path()), SchedulingQueue())

    assert snapshot.token_usage_total == {"host-a": 40, "host-b": 2}


def test_host_saturation_reflects_in_flight_over_max_concurrent_requests():
    registry = HostRegistry(new_registry_db_path())
    registry.register(
        "host-a",
        HostCapabilities(backend_type="lmstudio", context_window=8192, base_url="http://x"),
        HostCapacity(max_concurrent_requests=4),
        at=T0,
    )
    registry.acquire_slot("host-a")
    metrics = MetricsRegistry()

    snapshot = metrics.snapshot(T0, registry, SchedulingQueue())

    assert snapshot.host_saturation == {"host-a": 0.25}


def test_queue_depth_reflects_the_scheduling_queues_current_depth():
    queue = SchedulingQueue()
    queue.enqueue("req-1", session_id="session-1", priority=0, at=T0)
    metrics = MetricsRegistry()

    snapshot = metrics.snapshot(T0, HostRegistry(new_registry_db_path()), queue)

    assert snapshot.queue_depth == 1


def test_render_prometheus_on_a_fresh_registry_has_no_host_or_failover_lines():
    metrics = MetricsRegistry()

    output = metrics.render_prometheus(T0, HostRegistry(new_registry_db_path()), SchedulingQueue())

    assert output == (
        "# HELP llm_home_lab_availability_ratio Fraction of requests in the rolling window "
        "that did not return a 5xx status.\n"
        "# TYPE llm_home_lab_availability_ratio gauge\n"
        "llm_home_lab_availability_ratio 1.0\n"
        "# HELP llm_home_lab_request_latency_p95_ms P95 request latency in milliseconds over "
        "the rolling window.\n"
        "# TYPE llm_home_lab_request_latency_p95_ms gauge\n"
        "llm_home_lab_request_latency_p95_ms 0.0\n"
        "# HELP llm_home_lab_queue_depth Number of requests currently queued awaiting a free "
        "host slot.\n"
        "# TYPE llm_home_lab_queue_depth gauge\n"
        "llm_home_lab_queue_depth 0\n"
    )


def test_render_prometheus_includes_failover_host_and_token_lines_when_data_exists():
    registry = HostRegistry(new_registry_db_path())
    registry.register(
        "host-a",
        HostCapabilities(backend_type="lmstudio", context_window=8192, base_url="http://x"),
        HostCapacity(max_concurrent_requests=4),
        at=T0,
    )
    registry.acquire_slot("host-a")
    metrics = MetricsRegistry()
    metrics.record_failover_outcome(True, T0)
    metrics.record_token_usage("host-a", prompt_tokens=10, completion_tokens=5, at=T0)

    output = metrics.render_prometheus(T0, registry, SchedulingQueue())

    assert "llm_home_lab_failover_success_ratio 1.0" in output
    assert 'llm_home_lab_host_saturation_ratio{host_id="host-a"} 0.25' in output
    assert 'llm_home_lab_token_usage_total{host_id="host-a"} 15' in output

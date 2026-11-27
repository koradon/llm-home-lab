from datetime import UTC, datetime, timedelta

from llm_home_lab.diagnostics.metrics_parser import parse_metrics_text
from llm_home_lab.observability.completion_log import CompletionLog
from llm_home_lab.observability.metrics import MetricsRegistry
from llm_home_lab.registry.models import HostCapabilities, HostCapacity
from llm_home_lab.registry.registry import HostRegistry
from llm_home_lab.scheduling.queue import SchedulingQueue

T0 = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)


def test_host_registry_tracks_in_flight_per_model_and_preserves_across_reregister(tmp_path):
    registry = HostRegistry(str(tmp_path / "registry.db"))
    caps = HostCapabilities(backend_type="llamaserver", context_window=8192, base_url="http://n:1")
    registry.register("mini-1", caps, HostCapacity(max_concurrent_requests=8), at=T0)

    registry.acquire_slot_for_model("mini-1", model="gemma-e4b")
    registry.acquire_slot_for_model("mini-1", model="gemma-e4b")
    registry.acquire_slot_for_model("mini-1", model="eurollm-22b")
    host = registry.get("mini-1")
    assert host.in_flight == 3
    assert host.in_flight_by_model == {"gemma-e4b": 2, "eurollm-22b": 1}

    # Re-registering preserves per-model in-flight counts.
    registry.register("mini-1", caps, HostCapacity(max_concurrent_requests=10), at=T0)
    assert registry.get("mini-1").in_flight_by_model == {"gemma-e4b": 2, "eurollm-22b": 1}

    reg_id = registry.get("mini-1").registration_id
    registry.release_owned_slot("mini-1", reg_id, model="eurollm-22b")
    registry.release_owned_slot("mini-1", reg_id, model="gemma-e4b")
    assert registry.get("mini-1").in_flight == 1
    assert registry.get("mini-1").in_flight_by_model == {"gemma-e4b": 1}


def test_completion_log_tracks_per_model_throughput_and_sums_to_host():
    log = CompletionLog(window=timedelta(hours=1), clock=lambda: T0)
    at = T0 + timedelta(hours=1)
    for _ in range(3):
        log.record("mini-1", at, model="gemma-e4b")
    log.record("mini-1", at, model="eurollm-22b")

    assert log.tasks_per_hour("mini-1", at) == (
        log.tasks_per_hour_for_model("mini-1", "gemma-e4b", at)
        + log.tasks_per_hour_for_model("mini-1", "eurollm-22b", at)
    )
    assert log.tasks_per_hour_per_slot_for_model("mini-1", "eurollm-22b", at, 2) == 0.5
    assert set(log.tracked_models("mini-1")) == {"gemma-e4b", "eurollm-22b"}


def test_metrics_registry_emits_and_parser_reads_additive_per_model_series(tmp_path):
    registry = HostRegistry(str(tmp_path / "registry.db"))
    metrics = MetricsRegistry()
    queue = SchedulingQueue()

    metrics.record_host_completion("mini-1", 400.0, 200, 50, T0, model="gemma-e4b")
    metrics.record_host_completion("mini-1", 600.0, 300, 70, T0, model="gemma-e4b")
    metrics.record_host_completion("mini-1", 1400.0, 500, 120, T0, model="eurollm-22b")

    prom = metrics.render_prometheus(T0, registry, queue)
    parsed = parse_metrics_text(prom)

    # Existing host-level series remain intact and equal the sum of per-model counts.
    assert parsed.host_completions_total["mini-1"] == 3
    assert parsed.host_model_completions_total[("mini-1", "gemma-e4b")] == 2
    assert parsed.host_model_completions_total[("mini-1", "eurollm-22b")] == 1
    assert parsed.host_model_latency_ms_avg[("mini-1", "gemma-e4b")] == 500.0
    assert parsed.host_model_latency_ms_avg[("mini-1", "eurollm-22b")] == 1400.0
    assert parsed.host_model_prompt_tokens_min[("mini-1", "gemma-e4b")] == 200
    assert parsed.host_model_prompt_tokens_max[("mini-1", "gemma-e4b")] == 300
    assert parsed.host_model_completion_tokens_avg[("mini-1", "eurollm-22b")] == 120.0


def test_prometheus_labels_round_trip_escaped_host_and_model_values(tmp_path):
    registry = HostRegistry(str(tmp_path / "registry.db"))
    metrics = MetricsRegistry()
    host = 'host\\"\nname'
    model = 'model\\"\nname'
    metrics.record_host_completion(host, 400.0, 200, 50, T0, model=model)
    metrics.record_token_usage(host, 200, 50, T0)
    parsed = parse_metrics_text(metrics.render_prometheus(T0, registry, SchedulingQueue()))
    assert parsed.token_usage_total[host] == 250
    assert parsed.host_completions_total[host] == 1
    for field, value in (
        ("host_model_completions_total", 1),
        ("host_model_latency_ms_avg", 400),
        ("host_model_prompt_tokens_avg", 200),
        ("host_model_prompt_tokens_min", 200),
        ("host_model_prompt_tokens_max", 200),
        ("host_model_completion_tokens_avg", 50),
        ("host_model_completion_tokens_min", 50),
        ("host_model_completion_tokens_max", 50),
    ):
        assert getattr(parsed, field)[(host, model)] == value

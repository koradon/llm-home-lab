import math
from collections import defaultdict, deque
from datetime import datetime, timedelta

from llm_home_lab.observability.models import SliSnapshot
from llm_home_lab.registry.registry import HostRegistry
from llm_home_lab.scheduling.queue import SchedulingQueue


class MetricsRegistry:
    def __init__(self, window: timedelta = timedelta(minutes=5)) -> None:
        self._window = window
        self._requests: dict[str, deque[tuple[int, float, datetime]]] = defaultdict(deque)
        self._failover_outcomes: deque[tuple[bool, datetime]] = deque()
        self._token_usage_total: dict[str, int] = defaultdict(int)

    def record_request(
        self, endpoint: str, status_code: int, latency_ms: float, at: datetime
    ) -> None:
        samples = self._requests[endpoint]
        samples.append((status_code, latency_ms, at))
        self._evict(samples, at, timestamp_index=2)

    def record_failover_outcome(self, succeeded: bool, at: datetime) -> None:
        self._failover_outcomes.append((succeeded, at))
        self._evict(self._failover_outcomes, at, timestamp_index=1)

    def record_token_usage(
        self, host_id: str, prompt_tokens: int, completion_tokens: int, at: datetime
    ) -> None:
        self._token_usage_total[host_id] += prompt_tokens + completion_tokens

    def _evict(self, samples: deque, at: datetime, timestamp_index: int) -> None:
        while samples and at - samples[0][timestamp_index] > self._window:
            samples.popleft()

    def snapshot(
        self, at: datetime, registry: HostRegistry, scheduling_queue: SchedulingQueue
    ) -> SliSnapshot:
        for samples in self._requests.values():
            self._evict(samples, at, timestamp_index=2)
        all_samples = [sample for samples in self._requests.values() for sample in samples]

        if not all_samples:
            availability = 1.0
            p95_latency_ms = 0.0
        else:
            non_5xx = sum(1 for status_code, _, _ in all_samples if status_code < 500)
            availability = non_5xx / len(all_samples)
            p95_latency_ms = _percentile([latency_ms for _, latency_ms, _ in all_samples], 0.95)

        self._evict(self._failover_outcomes, at, timestamp_index=1)
        if not self._failover_outcomes:
            failover_success_rate = None
        else:
            successes = sum(1 for succeeded, _ in self._failover_outcomes if succeeded)
            failover_success_rate = successes / len(self._failover_outcomes)

        host_saturation = {
            host.host_id: host.in_flight / host.capacity.max_concurrent_requests
            for host in registry.hosts()
        }

        return SliSnapshot(
            availability=availability,
            p95_latency_ms=p95_latency_ms,
            failover_success_rate=failover_success_rate,
            host_saturation=host_saturation,
            queue_depth=scheduling_queue.depth(),
            token_usage_total=dict(self._token_usage_total),
        )

    def render_prometheus(
        self, at: datetime, registry: HostRegistry, scheduling_queue: SchedulingQueue
    ) -> str:
        snapshot = self.snapshot(at, registry, scheduling_queue)
        lines = [
            "# HELP llm_home_lab_availability_ratio Fraction of requests in the rolling window "
            "that did not return a 5xx status.",
            "# TYPE llm_home_lab_availability_ratio gauge",
            f"llm_home_lab_availability_ratio {snapshot.availability}",
            "# HELP llm_home_lab_request_latency_p95_ms P95 request latency in milliseconds "
            "over the rolling window.",
            "# TYPE llm_home_lab_request_latency_p95_ms gauge",
            f"llm_home_lab_request_latency_p95_ms {snapshot.p95_latency_ms}",
        ]

        if snapshot.failover_success_rate is not None:
            lines += [
                "# HELP llm_home_lab_failover_success_ratio Fraction of failover-involved "
                "requests that still succeeded.",
                "# TYPE llm_home_lab_failover_success_ratio gauge",
                f"llm_home_lab_failover_success_ratio {snapshot.failover_success_rate}",
            ]

        if snapshot.host_saturation:
            lines += [
                "# HELP llm_home_lab_host_saturation_ratio In-flight requests over "
                "max_concurrent_requests, per host.",
                "# TYPE llm_home_lab_host_saturation_ratio gauge",
            ]
            lines += [
                f'llm_home_lab_host_saturation_ratio{{host_id="{host_id}"}} {ratio}'
                for host_id, ratio in snapshot.host_saturation.items()
            ]

        lines += [
            "# HELP llm_home_lab_queue_depth Number of requests currently queued awaiting a "
            "free host slot.",
            "# TYPE llm_home_lab_queue_depth gauge",
            f"llm_home_lab_queue_depth {snapshot.queue_depth}",
        ]

        if snapshot.token_usage_total:
            lines += [
                "# HELP llm_home_lab_token_usage_total Cumulative prompt+completion tokens "
                "served, per host.",
                "# TYPE llm_home_lab_token_usage_total counter",
            ]
            lines += [
                f'llm_home_lab_token_usage_total{{host_id="{host_id}"}} {total}'
                for host_id, total in snapshot.token_usage_total.items()
            ]

        return "".join(f"{line}\n" for line in lines)


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = math.ceil(percentile * len(ordered)) - 1
    return ordered[max(0, min(index, len(ordered) - 1))]

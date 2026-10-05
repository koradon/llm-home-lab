from collections import defaultdict, deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

DEFAULT_THROUGHPUT_WINDOW = timedelta(hours=1)
MIN_OBSERVED = timedelta(seconds=60)


class CompletionLog:
    """In-memory record of processed tasks per host, behind the tasks/hour metrics.

    A "processed task" is one healthy completion — the same condition that feeds
    `MetricsRegistry.record_host_completion`: a non-streaming result that is not degenerate, or a
    stream that produced content and ended with `finish_reason == "stop"`. Failed and degenerate
    completions are not recorded at all.

    The window is the trailing `window` ending at the query time, half-open: a completion exactly
    `window` old is already out. Completions older than the window are dropped on each write. The
    log is not persisted: after an orchestrator restart the window starts empty.

    The rate divides the completions in the window by the time actually observed — the time since
    the log was created (tracking started), capped at the window and floored at `MIN_OBSERVED` so
    the first completion right after start is not extrapolated to an absurd hourly rate. Once a
    full window has elapsed this is the plain `count * 1h / window`.
    """

    def __init__(
        self,
        window: timedelta = DEFAULT_THROUGHPUT_WINDOW,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if window <= timedelta(0):
            raise ValueError("window must be positive")
        self._window = window
        self._started_at = clock()
        self._completions: dict[str, deque[datetime]] = defaultdict(deque)

    @property
    def window(self) -> timedelta:
        return self._window

    def record(self, host_id: str, at: datetime) -> None:
        cutoff = at - self._window
        completions = self._completions[host_id]
        completions.append(at)
        while completions and completions[0] <= cutoff:
            completions.popleft()

    def count(self, host_id: str, at: datetime) -> int:
        cutoff = at - self._window
        return sum(
            1 for completed_at in self._completions.get(host_id, ()) if cutoff < completed_at <= at
        )

    def tasks_per_hour(self, host_id: str, at: datetime) -> float:
        return self.count(host_id, at) * timedelta(hours=1) / self._observed(at)

    def _observed(self, at: datetime) -> timedelta:
        return max(min(at - self._started_at, self._window), MIN_OBSERVED)

    def tasks_per_hour_per_slot(
        self, host_id: str, at: datetime, total_slots: int | None
    ) -> float | None:
        if not total_slots:
            return None
        return self.tasks_per_hour(host_id, at) / total_slots

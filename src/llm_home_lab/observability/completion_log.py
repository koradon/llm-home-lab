from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta

from llm_home_lab.state.sqlite_base import SqliteStore

DEFAULT_THROUGHPUT_WINDOW = timedelta(hours=1)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS host_completions (
    host_id TEXT NOT NULL,
    completed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_host_completions_host_time
    ON host_completions (host_id, completed_at);
"""


class CompletionLog:
    """Durable record of processed tasks per host, behind the tasks/hour metrics.

    A "processed task" is one healthy completion — the same condition that feeds
    `MetricsRegistry.record_host_completion`: a non-streaming result that is not degenerate, or a
    stream that produced content and ended with `finish_reason == "stop"`. Failed and degenerate
    completions are not recorded at all.

    The window is the trailing `window` ending at the query time, half-open: a completion exactly
    `window` old is already out. Rows are persisted in SQLite (when `db_path` is given) so the
    rate survives an orchestrator restart; rows older than the window are pruned on each write.
    Without `db_path` the log is in-memory only.
    """

    def __init__(
        self, window: timedelta = DEFAULT_THROUGHPUT_WINDOW, db_path: str | None = None
    ) -> None:
        if window <= timedelta(0):
            raise ValueError("window must be positive")
        self._window = window
        self._store = _CompletionStore(db_path) if db_path is not None else None
        self._in_memory: dict[str, deque[datetime]] = defaultdict(deque)

    @property
    def window(self) -> timedelta:
        return self._window

    def record(self, host_id: str, at: datetime) -> None:
        cutoff = at - self._window
        if self._store is not None:
            self._store.insert_and_prune(host_id, at, cutoff)
            return

        completions = self._in_memory[host_id]
        completions.append(at)
        while completions and completions[0] <= cutoff:
            completions.popleft()

    def count(self, host_id: str, at: datetime) -> int:
        cutoff = at - self._window
        if self._store is not None:
            return self._store.count_after(host_id, cutoff, at)
        return sum(
            1 for completed_at in self._in_memory.get(host_id, ()) if cutoff < completed_at <= at
        )

    def tasks_per_hour(self, host_id: str, at: datetime) -> float:
        return self.count(host_id, at) * timedelta(hours=1) / self._window

    def tasks_per_hour_per_slot(
        self, host_id: str, at: datetime, total_slots: int | None
    ) -> float | None:
        if not total_slots:
            return None
        return self.tasks_per_hour(host_id, at) / total_slots


class _CompletionStore(SqliteStore):
    def __init__(self, db_path: str) -> None:
        super().__init__(db_path, _SCHEMA)

    def insert_and_prune(self, host_id: str, at: datetime, cutoff: datetime) -> None:
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO host_completions (host_id, completed_at) VALUES (?, ?)",
                (host_id, _utc(at)),
            )
            conn.execute("DELETE FROM host_completions WHERE completed_at <= ?", (_utc(cutoff),))

    def count_after(self, host_id: str, cutoff: datetime, at: datetime) -> int:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM host_completions "
                "WHERE host_id = ? AND completed_at > ? AND completed_at <= ?",
                (host_id, _utc(cutoff), _utc(at)),
            ).fetchone()
        return int(row[0])


def _utc(moment: datetime) -> str:
    # Fixed-width UTC ISO strings so SQLite's text comparison orders them chronologically.
    return moment.astimezone(UTC).isoformat(timespec="microseconds")

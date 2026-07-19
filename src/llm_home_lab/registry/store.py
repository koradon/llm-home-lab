import json
from dataclasses import asdict
from datetime import datetime

from llm_home_lab.registry.models import HostCapabilities, HostCapacity, HostInfo
from llm_home_lab.state.sqlite_base import SqliteStore

_SCHEMA = """
CREATE TABLE IF NOT EXISTS hosts (
    host_id TEXT PRIMARY KEY,
    capabilities TEXT NOT NULL,
    capacity TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
"""


class HostRegistryStore(SqliteStore):
    def __init__(self, db_path: str) -> None:
        super().__init__(db_path, _SCHEMA)

    def load_hosts(self) -> list[HostInfo]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT host_id, capabilities, capacity, last_seen FROM hosts"
            ).fetchall()
        return [
            HostInfo(
                host_id=host_id,
                capabilities=HostCapabilities(**json.loads(capabilities_json)),
                capacity=HostCapacity(**json.loads(capacity_json)),
                in_flight=0,
                last_seen=datetime.fromisoformat(last_seen),
            )
            for host_id, capabilities_json, capacity_json, last_seen in rows
        ]

    def upsert(
        self,
        host_id: str,
        capabilities: HostCapabilities,
        capacity: HostCapacity,
        last_seen: datetime,
    ) -> None:
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO hosts (host_id, capabilities, capacity, last_seen) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(host_id) DO UPDATE SET "
                "capabilities = excluded.capabilities, "
                "capacity = excluded.capacity, "
                "last_seen = excluded.last_seen",
                (
                    host_id,
                    json.dumps(asdict(capabilities)),
                    json.dumps(asdict(capacity)),
                    last_seen.isoformat(),
                ),
            )

    def update_last_seen(self, host_id: str, last_seen: datetime) -> None:
        with self._connection() as conn:
            conn.execute(
                "UPDATE hosts SET last_seen = ? WHERE host_id = ?",
                (last_seen.isoformat(), host_id),
            )

    def delete(self, host_id: str) -> None:
        with self._connection() as conn:
            conn.execute("DELETE FROM hosts WHERE host_id = ?", (host_id,))

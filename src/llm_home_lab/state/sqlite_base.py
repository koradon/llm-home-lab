import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from llm_home_lab.state.models import SessionNotFoundError


class SqliteStore:
    def __init__(self, db_path: str, schema: str) -> None:
        self._db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            conn.executescript(schema)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _require_session(self, conn: sqlite3.Connection, session_id: str) -> None:
        row = conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            raise SessionNotFoundError(session_id)

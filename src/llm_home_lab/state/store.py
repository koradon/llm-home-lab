import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from llm_home_lab.state.models import (
    InvalidSummaryError,
    Session,
    SessionNotFoundError,
    StoredMessage,
    Summary,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (session_id, seq)
);
CREATE TABLE IF NOT EXISTS summaries (
    session_id TEXT PRIMARY KEY,
    covers_up_to_seq INTEGER NOT NULL,
    summary_text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class SessionStore:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            conn.executescript(_SCHEMA)

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

    def create_session(self, session_id: str, created_at: str) -> None:
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO sessions (id, created_at) VALUES (?, ?)", (session_id, created_at)
            )

    def append_message(self, session_id: str, role: str, content: str, created_at: str) -> None:
        with self._connection() as conn:
            self._require_session(conn, session_id)
            row = conn.execute(
                "SELECT MAX(seq) FROM messages WHERE session_id = ?", (session_id,)
            ).fetchone()
            next_seq = (row[0] or 0) + 1
            conn.execute(
                "INSERT INTO messages (session_id, seq, role, content, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, next_seq, role, content, created_at),
            )

    def read_session(self, session_id: str) -> Session:
        with self._connection() as conn:
            self._require_session(conn, session_id)

            summary_row = conn.execute(
                "SELECT covers_up_to_seq, summary_text, created_at FROM summaries "
                "WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            summary = None
            covers_up_to_seq = 0
            if summary_row is not None:
                covers_up_to_seq, summary_text, summary_created_at = summary_row
                summary = Summary(
                    covers_up_to_seq=covers_up_to_seq,
                    summary_text=summary_text,
                    created_at=summary_created_at,
                )

            message_rows = conn.execute(
                "SELECT seq, role, content, created_at FROM messages "
                "WHERE session_id = ? AND seq > ? ORDER BY seq",
                (session_id, covers_up_to_seq),
            ).fetchall()
            messages = [
                StoredMessage(seq=r[0], role=r[1], content=r[2], created_at=r[3])
                for r in message_rows
            ]
            return Session(session_id=session_id, messages=messages, summary=summary)

    def summarize(
        self, session_id: str, summary_text: str, covers_up_to_seq: int, created_at: str
    ) -> None:
        with self._connection() as conn:
            self._require_session(conn, session_id)

            row = conn.execute(
                "SELECT MAX(seq) FROM messages WHERE session_id = ?", (session_id,)
            ).fetchone()
            max_seq = row[0] or 0
            if covers_up_to_seq > max_seq:
                raise InvalidSummaryError(session_id, covers_up_to_seq, max_seq)

            conn.execute(
                "INSERT INTO summaries (session_id, covers_up_to_seq, summary_text, created_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "covers_up_to_seq = excluded.covers_up_to_seq, "
                "summary_text = excluded.summary_text, "
                "created_at = excluded.created_at",
                (session_id, covers_up_to_seq, summary_text, created_at),
            )

    def trim(self, session_id: str) -> int:
        with self._connection() as conn:
            self._require_session(conn, session_id)

            row = conn.execute(
                "SELECT covers_up_to_seq FROM summaries WHERE session_id = ?", (session_id,)
            ).fetchone()
            if row is None:
                return 0

            cursor = conn.execute(
                "DELETE FROM messages WHERE session_id = ? AND seq <= ?", (session_id, row[0])
            )
            return cursor.rowcount

import json
import sqlite3
from typing import Any

from llm_home_lab.state.models import FilesystemInvocation, TerminalInvocation, TerminalState
from llm_home_lab.state.schema import SESSIONS_TABLE_SCHEMA
from llm_home_lab.state.sqlite_base import SqliteStore

_SCHEMA = (
    SESSIONS_TABLE_SCHEMA
    + """
CREATE TABLE IF NOT EXISTS tool_invocations (
    session_id TEXT NOT NULL,
    tool_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (session_id, seq)
);
"""
)


class ToolStateStore(SqliteStore):
    def __init__(self, db_path: str) -> None:
        super().__init__(db_path, _SCHEMA)

    def _insert_invocation(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        tool_id: str,
        payload: dict[str, Any],
        created_at: str,
    ) -> None:
        row = conn.execute(
            "SELECT MAX(seq) FROM tool_invocations WHERE session_id = ?", (session_id,)
        ).fetchone()
        next_seq = (row[0] or 0) + 1
        conn.execute(
            "INSERT INTO tool_invocations (session_id, tool_id, seq, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, tool_id, next_seq, json.dumps(payload), created_at),
        )

    def _read_history_rows(
        self, conn: sqlite3.Connection, session_id: str, tool_id: str
    ) -> list[tuple[int, dict[str, Any], str]]:
        rows = conn.execute(
            "SELECT seq, payload, created_at FROM tool_invocations "
            "WHERE session_id = ? AND tool_id = ? ORDER BY seq",
            (session_id, tool_id),
        ).fetchall()
        return [(seq, json.loads(payload), created_at) for seq, payload, created_at in rows]

    def record_terminal(
        self,
        session_id: str,
        command: str,
        cwd: str,
        exit_code: int,
        output: str,
        env: dict[str, str],
        running_processes: list[str],
        created_at: str,
    ) -> None:
        with self._connection() as conn:
            self._require_session(conn, session_id)
            self._insert_invocation(
                conn,
                session_id,
                "terminal",
                {
                    "command": command,
                    "cwd": cwd,
                    "exit_code": exit_code,
                    "output": output,
                    "env": env,
                    "running_processes": running_processes,
                },
                created_at,
            )

    def read_terminal_history(self, session_id: str) -> list[TerminalInvocation]:
        with self._connection() as conn:
            self._require_session(conn, session_id)
            rows = self._read_history_rows(conn, session_id, "terminal")
            return [
                TerminalInvocation(
                    seq=seq,
                    command=payload["command"],
                    cwd=payload["cwd"],
                    exit_code=payload["exit_code"],
                    output=payload["output"],
                    env=payload["env"],
                    running_processes=payload["running_processes"],
                    created_at=created_at,
                )
                for seq, payload, created_at in rows
            ]

    def read_terminal_state(self, session_id: str) -> TerminalState | None:
        with self._connection() as conn:
            self._require_session(conn, session_id)
            rows = self._read_history_rows(conn, session_id, "terminal")
            if not rows:
                return None
            seq, payload, _created_at = rows[-1]
            return TerminalState(
                cwd=payload["cwd"],
                env=payload["env"],
                running_processes=payload["running_processes"],
                as_of_seq=seq,
            )

    def record_filesystem(
        self,
        session_id: str,
        operation: str,
        path: str,
        result: str,
        created_at: str,
    ) -> None:
        with self._connection() as conn:
            self._require_session(conn, session_id)
            self._insert_invocation(
                conn,
                session_id,
                "filesystem",
                {"operation": operation, "path": path, "result": result},
                created_at,
            )

    def read_filesystem_history(self, session_id: str) -> list[FilesystemInvocation]:
        with self._connection() as conn:
            self._require_session(conn, session_id)
            rows = self._read_history_rows(conn, session_id, "filesystem")
            return [
                FilesystemInvocation(
                    seq=seq,
                    operation=payload["operation"],
                    path=payload["path"],
                    result=payload["result"],
                    created_at=created_at,
                )
                for seq, payload, created_at in rows
            ]

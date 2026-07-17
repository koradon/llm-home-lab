import json

from llm_home_lab.state.models import RunStatus, WorkspaceSnapshot
from llm_home_lab.state.schema import SESSIONS_TABLE_SCHEMA
from llm_home_lab.state.sqlite_base import SqliteStore

_SCHEMA = (
    SESSIONS_TABLE_SCHEMA
    + """
CREATE TABLE IF NOT EXISTS workspace_snapshots (
    session_id TEXT PRIMARY KEY,
    branch TEXT NOT NULL,
    git_diff TEXT NOT NULL,
    diff_truncated INTEGER NOT NULL,
    open_files TEXT NOT NULL,
    open_files_truncated INTEGER NOT NULL,
    test_status TEXT,
    created_at TEXT NOT NULL
);
"""
)


class WorkspaceStore(SqliteStore):
    def __init__(self, db_path: str) -> None:
        super().__init__(db_path, _SCHEMA)

    def capture(
        self,
        session_id: str,
        branch: str,
        git_diff: str,
        diff_truncated: bool,
        open_files: list[str],
        open_files_truncated: bool,
        test_status: RunStatus | None,
        created_at: str,
    ) -> None:
        with self._connection() as conn:
            self._require_session(conn, session_id)
            test_status_json = (
                json.dumps(
                    {
                        "passed": test_status.passed,
                        "failed": test_status.failed,
                        "total": test_status.total,
                        "summary": test_status.summary,
                    }
                )
                if test_status is not None
                else None
            )
            conn.execute(
                "INSERT INTO workspace_snapshots "
                "(session_id, branch, git_diff, diff_truncated, open_files, "
                "open_files_truncated, test_status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "branch = excluded.branch, "
                "git_diff = excluded.git_diff, "
                "diff_truncated = excluded.diff_truncated, "
                "open_files = excluded.open_files, "
                "open_files_truncated = excluded.open_files_truncated, "
                "test_status = excluded.test_status, "
                "created_at = excluded.created_at",
                (
                    session_id,
                    branch,
                    git_diff,
                    int(diff_truncated),
                    json.dumps(open_files),
                    int(open_files_truncated),
                    test_status_json,
                    created_at,
                ),
            )

    def read(self, session_id: str) -> WorkspaceSnapshot | None:
        with self._connection() as conn:
            self._require_session(conn, session_id)
            row = conn.execute(
                "SELECT branch, git_diff, diff_truncated, open_files, open_files_truncated, "
                "test_status, created_at FROM workspace_snapshots WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            (
                branch,
                git_diff,
                diff_truncated,
                open_files,
                open_files_truncated,
                test_status_json,
                created_at,
            ) = row
            test_status = (
                RunStatus(**json.loads(test_status_json)) if test_status_json is not None else None
            )
            return WorkspaceSnapshot(
                session_id=session_id,
                branch=branch,
                git_diff=git_diff,
                diff_truncated=bool(diff_truncated),
                open_files=json.loads(open_files),
                open_files_truncated=bool(open_files_truncated),
                test_status=test_status,
                created_at=created_at,
            )

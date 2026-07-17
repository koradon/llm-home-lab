import asyncio
import os
from datetime import UTC, datetime

from llm_home_lab.state.models import RunStatus, WorkspaceSnapshot
from llm_home_lab.state.session_manager import DEFAULT_SESSION_STORE_PATH
from llm_home_lab.state.workspace_store import WorkspaceStore

DEFAULT_DIFF_MAX_CHARS = 20_000
DEFAULT_OPEN_FILES_MAX = 200


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _truncate_text(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    cut = len(text) - max_chars
    return text[:max_chars] + f"... [truncated {cut} chars]", True


def _truncate_list(items: list[str], max_count: int) -> tuple[list[str], bool]:
    if len(items) <= max_count:
        return items, False
    return items[:max_count], True


class WorkspaceManager:
    def __init__(
        self,
        db_path: str,
        diff_max_chars: int = DEFAULT_DIFF_MAX_CHARS,
        open_files_max: int = DEFAULT_OPEN_FILES_MAX,
    ) -> None:
        self._store = WorkspaceStore(db_path)
        self._diff_max_chars = diff_max_chars
        self._open_files_max = open_files_max

    @classmethod
    def from_env(cls) -> "WorkspaceManager":
        return cls(
            os.environ.get("SESSION_STORE_PATH", DEFAULT_SESSION_STORE_PATH),
            diff_max_chars=int(os.environ.get("WORKSPACE_DIFF_MAX_CHARS", DEFAULT_DIFF_MAX_CHARS)),
            open_files_max=int(os.environ.get("WORKSPACE_OPEN_FILES_MAX", DEFAULT_OPEN_FILES_MAX)),
        )

    async def capture(
        self,
        session_id: str,
        branch: str,
        git_diff: str,
        open_files: list[str],
        test_status: RunStatus | None = None,
    ) -> None:
        diff, diff_truncated = _truncate_text(git_diff, self._diff_max_chars)
        files, files_truncated = _truncate_list(open_files, self._open_files_max)
        await asyncio.to_thread(
            self._store.capture,
            session_id,
            branch,
            diff,
            diff_truncated,
            files,
            files_truncated,
            test_status,
            _now(),
        )

    async def read(self, session_id: str) -> WorkspaceSnapshot | None:
        return await asyncio.to_thread(self._store.read, session_id)

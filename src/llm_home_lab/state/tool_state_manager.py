import asyncio
import os
from datetime import UTC, datetime

from llm_home_lab.state.models import FilesystemInvocation, TerminalInvocation, TerminalState
from llm_home_lab.state.session_manager import DEFAULT_SESSION_STORE_PATH
from llm_home_lab.state.tool_state_store import ToolStateStore


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ToolStateManager:
    def __init__(self, db_path: str) -> None:
        self._store = ToolStateStore(db_path)

    @classmethod
    def from_env(cls) -> "ToolStateManager":
        return cls(os.environ.get("SESSION_STORE_PATH", DEFAULT_SESSION_STORE_PATH))

    async def record_terminal_invocation(
        self,
        session_id: str,
        command: str,
        cwd: str,
        exit_code: int,
        output: str,
        env: dict[str, str] | None = None,
        running_processes: list[str] | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._store.record_terminal,
            session_id,
            command,
            cwd,
            exit_code,
            output,
            env or {},
            running_processes or [],
            _now(),
        )

    async def read_terminal_history(self, session_id: str) -> list[TerminalInvocation]:
        return await asyncio.to_thread(self._store.read_terminal_history, session_id)

    async def read_terminal_state(self, session_id: str) -> TerminalState | None:
        return await asyncio.to_thread(self._store.read_terminal_state, session_id)

    async def record_filesystem_invocation(
        self, session_id: str, operation: str, path: str, result: str
    ) -> None:
        await asyncio.to_thread(
            self._store.record_filesystem, session_id, operation, path, result, _now()
        )

    async def read_filesystem_history(self, session_id: str) -> list[FilesystemInvocation]:
        return await asyncio.to_thread(self._store.read_filesystem_history, session_id)

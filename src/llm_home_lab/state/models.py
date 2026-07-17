from dataclasses import dataclass


class SessionError(Exception):
    """Base class for session manager failures."""


class SessionNotFoundError(SessionError):
    def __init__(self, session_id: str) -> None:
        super().__init__(f"session not found: {session_id}")
        self.session_id = session_id


class InvalidSummaryError(SessionError):
    def __init__(self, session_id: str, covers_up_to_seq: int, max_seq: int) -> None:
        super().__init__(
            f"covers_up_to_seq {covers_up_to_seq} exceeds highest message seq {max_seq} "
            f"for session {session_id}"
        )
        self.session_id = session_id
        self.covers_up_to_seq = covers_up_to_seq
        self.max_seq = max_seq


@dataclass
class StoredMessage:
    seq: int
    role: str
    content: str
    created_at: str


@dataclass
class Summary:
    covers_up_to_seq: int
    summary_text: str
    created_at: str


@dataclass
class Session:
    session_id: str
    messages: list[StoredMessage]
    summary: Summary | None = None


@dataclass
class RunStatus:
    passed: int
    failed: int
    total: int
    summary: str | None = None


@dataclass
class WorkspaceSnapshot:
    session_id: str
    branch: str
    git_diff: str
    diff_truncated: bool
    open_files: list[str]
    open_files_truncated: bool
    test_status: RunStatus | None
    created_at: str


@dataclass
class TerminalInvocation:
    seq: int
    command: str
    cwd: str
    exit_code: int
    output: str
    env: dict[str, str]
    running_processes: list[str]
    created_at: str


@dataclass
class FilesystemInvocation:
    seq: int
    operation: str
    path: str
    result: str
    created_at: str


@dataclass
class TerminalState:
    cwd: str
    env: dict[str, str]
    running_processes: list[str]
    as_of_seq: int

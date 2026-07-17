import pytest

from llm_home_lab.state.models import RunStatus, SessionNotFoundError
from llm_home_lab.state.session_manager import SessionManager
from llm_home_lab.state.workspace_manager import WorkspaceManager


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "sessions.db")


async def test_capture_then_read_round_trips(db_path):
    sessions = SessionManager(db_path)
    session_id = await sessions.create_session()
    workspaces = WorkspaceManager(db_path)

    await workspaces.capture(
        session_id, branch="main", git_diff="diff --git a/f.py", open_files=["f.py", "g.py"]
    )
    snapshot = await workspaces.read(session_id)

    assert snapshot.branch == "main"
    assert snapshot.git_diff == "diff --git a/f.py"
    assert snapshot.open_files == ["f.py", "g.py"]


async def test_reading_before_any_capture_returns_none(db_path):
    sessions = SessionManager(db_path)
    session_id = await sessions.create_session()
    workspaces = WorkspaceManager(db_path)

    snapshot = await workspaces.read(session_id)

    assert snapshot is None


async def test_capturing_again_replaces_the_previous_snapshot(db_path):
    sessions = SessionManager(db_path)
    session_id = await sessions.create_session()
    workspaces = WorkspaceManager(db_path)
    await workspaces.capture(session_id, branch="main", git_diff="d1", open_files=["a.py"])

    await workspaces.capture(session_id, branch="feature/x", git_diff="d2", open_files=["b.py"])

    snapshot = await workspaces.read(session_id)

    assert snapshot.branch == "feature/x"
    assert snapshot.open_files == ["b.py"]


async def test_capture_with_test_status_round_trips(db_path):
    sessions = SessionManager(db_path)
    session_id = await sessions.create_session()
    workspaces = WorkspaceManager(db_path)
    status = RunStatus(passed=8, failed=1, total=9, summary="1 failure in test_foo")

    await workspaces.capture(
        session_id, branch="main", git_diff="d", open_files=[], test_status=status
    )
    snapshot = await workspaces.read(session_id)

    assert snapshot.test_status == status


async def test_capture_without_test_status_reports_none(db_path):
    sessions = SessionManager(db_path)
    session_id = await sessions.create_session()
    workspaces = WorkspaceManager(db_path)

    await workspaces.capture(session_id, branch="main", git_diff="d", open_files=[])
    snapshot = await workspaces.read(session_id)

    assert snapshot.test_status is None


async def test_oversized_diff_is_truncated_not_rejected(db_path):
    sessions = SessionManager(db_path)
    session_id = await sessions.create_session()
    workspaces = WorkspaceManager(db_path, diff_max_chars=10)

    await workspaces.capture(session_id, branch="main", git_diff="0123456789extra", open_files=[])
    snapshot = await workspaces.read(session_id)

    assert len(snapshot.git_diff) <= 10 + len("... [truncated 5 chars]")
    assert snapshot.git_diff.startswith("0123456789")
    assert snapshot.diff_truncated is True


async def test_diff_within_limit_is_not_truncated(db_path):
    sessions = SessionManager(db_path)
    session_id = await sessions.create_session()
    workspaces = WorkspaceManager(db_path, diff_max_chars=10)

    await workspaces.capture(session_id, branch="main", git_diff="short", open_files=[])
    snapshot = await workspaces.read(session_id)

    assert snapshot.git_diff == "short"
    assert snapshot.diff_truncated is False


async def test_oversized_open_files_list_is_truncated_not_rejected(db_path):
    sessions = SessionManager(db_path)
    session_id = await sessions.create_session()
    workspaces = WorkspaceManager(db_path, open_files_max=2)

    await workspaces.capture(
        session_id, branch="main", git_diff="d", open_files=["a.py", "b.py", "c.py"]
    )
    snapshot = await workspaces.read(session_id)

    assert snapshot.open_files == ["a.py", "b.py"]
    assert snapshot.open_files_truncated is True


async def test_open_files_within_limit_is_not_truncated(db_path):
    sessions = SessionManager(db_path)
    session_id = await sessions.create_session()
    workspaces = WorkspaceManager(db_path, open_files_max=2)

    await workspaces.capture(session_id, branch="main", git_diff="d", open_files=["a.py"])
    snapshot = await workspaces.read(session_id)

    assert snapshot.open_files == ["a.py"]
    assert snapshot.open_files_truncated is False


async def test_from_env_uses_session_store_path_and_limits(tmp_path, monkeypatch):
    env_path = tmp_path / "env_sessions.db"
    monkeypatch.setenv("SESSION_STORE_PATH", str(env_path))
    monkeypatch.setenv("WORKSPACE_DIFF_MAX_CHARS", "10")
    monkeypatch.setenv("WORKSPACE_OPEN_FILES_MAX", "1")
    sessions = SessionManager.from_env()
    session_id = await sessions.create_session()
    workspaces = WorkspaceManager.from_env()

    await workspaces.capture(
        session_id, branch="main", git_diff="0123456789extra", open_files=["a.py", "b.py"]
    )
    snapshot = await workspaces.read(session_id)

    assert snapshot.diff_truncated is True
    assert snapshot.open_files == ["a.py"]


async def test_capturing_for_unknown_session_raises(db_path):
    workspaces = WorkspaceManager(db_path)

    with pytest.raises(SessionNotFoundError):
        await workspaces.capture("missing", branch="main", git_diff="d", open_files=[])


async def test_reading_unknown_session_raises(db_path):
    workspaces = WorkspaceManager(db_path)

    with pytest.raises(SessionNotFoundError):
        await workspaces.read("missing")

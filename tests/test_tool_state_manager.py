import pytest

from llm_home_lab.state.models import SessionNotFoundError
from llm_home_lab.state.session_manager import SessionManager
from llm_home_lab.state.tool_state_manager import ToolStateManager


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "sessions.db")


async def test_recording_and_reading_terminal_history_round_trips(db_path):
    sessions = SessionManager(db_path)
    session_id = await sessions.create_session()
    tools = ToolStateManager(db_path)

    await tools.record_terminal_invocation(
        session_id, command="npm test", cwd="/repo", exit_code=0, output="ok"
    )
    history = await tools.read_terminal_history(session_id)

    assert len(history) == 1
    assert history[0].command == "npm test"
    assert history[0].cwd == "/repo"
    assert history[0].exit_code == 0
    assert history[0].output == "ok"


async def test_recording_and_reading_filesystem_history_round_trips(db_path):
    sessions = SessionManager(db_path)
    session_id = await sessions.create_session()
    tools = ToolStateManager(db_path)

    await tools.record_filesystem_invocation(
        session_id, operation="write", path="app.py", result="ok"
    )
    history = await tools.read_filesystem_history(session_id)

    assert len(history) == 1
    assert history[0].operation == "write"
    assert history[0].path == "app.py"
    assert history[0].result == "ok"


async def test_terminal_and_filesystem_invocations_share_one_chronological_sequence(db_path):
    sessions = SessionManager(db_path)
    session_id = await sessions.create_session()
    tools = ToolStateManager(db_path)
    await tools.record_terminal_invocation(
        session_id, command="git status", cwd="/repo", exit_code=0, output="clean"
    )
    await tools.record_filesystem_invocation(
        session_id, operation="write", path="app.py", result="ok"
    )
    await tools.record_terminal_invocation(
        session_id, command="npm test", cwd="/repo", exit_code=0, output="ok"
    )

    terminal_history = await tools.read_terminal_history(session_id)
    filesystem_history = await tools.read_filesystem_history(session_id)

    assert [t.command for t in terminal_history] == ["git status", "npm test"]
    assert [t.seq for t in terminal_history] == [1, 3]
    assert [f.path for f in filesystem_history] == ["app.py"]
    assert [f.seq for f in filesystem_history] == [2]


async def test_terminal_state_reflects_the_most_recent_invocation(db_path):
    sessions = SessionManager(db_path)
    session_id = await sessions.create_session()
    tools = ToolStateManager(db_path)
    await tools.record_terminal_invocation(
        session_id, command="cd a", cwd="/repo/a", exit_code=0, output="", env={"X": "1"}
    )
    await tools.record_terminal_invocation(
        session_id, command="cd b", cwd="/repo/b", exit_code=0, output="", env={"X": "2"}
    )

    state = await tools.read_terminal_state(session_id)

    assert state.cwd == "/repo/b"
    assert state.env == {"X": "2"}
    assert state.as_of_seq == 2


async def test_terminal_state_is_absent_before_any_terminal_invocation(db_path):
    sessions = SessionManager(db_path)
    session_id = await sessions.create_session()
    tools = ToolStateManager(db_path)

    state = await tools.read_terminal_state(session_id)

    assert state is None


async def test_empty_history_is_not_an_error(db_path):
    sessions = SessionManager(db_path)
    session_id = await sessions.create_session()
    tools = ToolStateManager(db_path)

    history = await tools.read_terminal_history(session_id)

    assert history == []


async def test_from_env_uses_session_store_path(tmp_path, monkeypatch):
    env_path = tmp_path / "env_sessions.db"
    monkeypatch.setenv("SESSION_STORE_PATH", str(env_path))
    sessions = SessionManager.from_env()
    session_id = await sessions.create_session()
    tools = ToolStateManager.from_env()

    await tools.record_terminal_invocation(
        session_id, command="ls", cwd="/", exit_code=0, output=""
    )

    assert env_path.exists()


async def test_recording_terminal_invocation_for_unknown_session_raises(db_path):
    tools = ToolStateManager(db_path)

    with pytest.raises(SessionNotFoundError):
        await tools.record_terminal_invocation(
            "missing", command="ls", cwd="/", exit_code=0, output=""
        )


async def test_recording_filesystem_invocation_for_unknown_session_raises(db_path):
    tools = ToolStateManager(db_path)

    with pytest.raises(SessionNotFoundError):
        await tools.record_filesystem_invocation("missing", operation="read", path="a", result="")


async def test_reading_terminal_history_for_unknown_session_raises(db_path):
    tools = ToolStateManager(db_path)

    with pytest.raises(SessionNotFoundError):
        await tools.read_terminal_history("missing")


async def test_reading_filesystem_history_for_unknown_session_raises(db_path):
    tools = ToolStateManager(db_path)

    with pytest.raises(SessionNotFoundError):
        await tools.read_filesystem_history("missing")


async def test_reading_terminal_state_for_unknown_session_raises(db_path):
    tools = ToolStateManager(db_path)

    with pytest.raises(SessionNotFoundError):
        await tools.read_terminal_state("missing")

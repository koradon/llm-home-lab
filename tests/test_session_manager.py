import pytest

from llm_home_lab.state.models import InvalidSummaryError, SessionNotFoundError
from llm_home_lab.state.session_manager import SessionManager


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "sessions.db")


async def test_new_session_has_no_messages_and_no_summary(db_path):
    manager = SessionManager(db_path)

    session_id = await manager.create_session()
    session = await manager.read_session(session_id)

    assert session.messages == []
    assert session.summary is None


async def test_messages_are_read_back_in_append_order(db_path):
    manager = SessionManager(db_path)
    session_id = await manager.create_session()
    await manager.append_message(session_id, "user", "Hi")
    await manager.append_message(session_id, "assistant", "Hello")

    session = await manager.read_session(session_id)

    assert [m.content for m in session.messages] == ["Hi", "Hello"]
    assert [m.seq for m in session.messages] == [1, 2]


async def test_append_to_unknown_session_raises(db_path):
    manager = SessionManager(db_path)

    with pytest.raises(SessionNotFoundError):
        await manager.append_message("missing", "user", "hi")


async def test_reading_unknown_session_raises(db_path):
    manager = SessionManager(db_path)

    with pytest.raises(SessionNotFoundError):
        await manager.read_session("missing")


async def test_summarize_hides_covered_messages(db_path):
    manager = SessionManager(db_path)
    session_id = await manager.create_session()
    await manager.append_message(session_id, "user", "Hi")
    await manager.append_message(session_id, "assistant", "Hello")

    await manager.summarize(session_id, "greeting exchange", covers_up_to_seq=2)
    session = await manager.read_session(session_id)

    assert session.summary.summary_text == "greeting exchange"
    assert session.messages == []


async def test_summarize_beyond_highest_seq_raises_and_stores_nothing(db_path):
    manager = SessionManager(db_path)
    session_id = await manager.create_session()
    await manager.append_message(session_id, "user", "Hi")

    with pytest.raises(InvalidSummaryError):
        await manager.summarize(session_id, "bogus", covers_up_to_seq=5)

    session = await manager.read_session(session_id)

    assert session.summary is None


async def test_summarize_then_trim_removes_covered_messages(db_path):
    manager = SessionManager(db_path)
    session_id = await manager.create_session()
    await manager.append_message(session_id, "user", "Hi")
    await manager.append_message(session_id, "assistant", "Hello")
    await manager.summarize(session_id, "greeting exchange", covers_up_to_seq=2)

    deleted = await manager.trim(session_id)

    assert deleted == 2


async def test_trim_with_no_summary_is_a_no_op(db_path):
    manager = SessionManager(db_path)
    session_id = await manager.create_session()
    await manager.append_message(session_id, "user", "Hi")

    deleted = await manager.trim(session_id)

    assert deleted == 0


async def test_session_state_survives_restart(db_path):
    manager_a = SessionManager(db_path)
    session_id = await manager_a.create_session()
    await manager_a.append_message(session_id, "user", "Hi")
    await manager_a.summarize(session_id, "greeting", covers_up_to_seq=1)
    manager_b = SessionManager(db_path)

    session = await manager_b.read_session(session_id)

    assert session.summary.summary_text == "greeting"
    assert session.messages == []


async def test_from_env_uses_session_store_path(tmp_path, monkeypatch):
    env_path = tmp_path / "env_sessions.db"
    monkeypatch.setenv("SESSION_STORE_PATH", str(env_path))
    manager = SessionManager.from_env()

    await manager.create_session()

    assert env_path.exists()

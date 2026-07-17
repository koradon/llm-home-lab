import asyncio
import os
import uuid
from datetime import UTC, datetime

from llm_home_lab.state.models import Session
from llm_home_lab.state.store import SessionStore

DEFAULT_SESSION_STORE_PATH = "./data/sessions.db"


def _now() -> str:
    return datetime.now(UTC).isoformat()


class SessionManager:
    def __init__(self, db_path: str) -> None:
        self._store = SessionStore(db_path)

    @classmethod
    def from_env(cls) -> "SessionManager":
        return cls(os.environ.get("SESSION_STORE_PATH", DEFAULT_SESSION_STORE_PATH))

    async def create_session(self) -> str:
        session_id = uuid.uuid4().hex
        await asyncio.to_thread(self._store.create_session, session_id, _now())
        return session_id

    async def append_message(self, session_id: str, role: str, content: str) -> None:
        await asyncio.to_thread(self._store.append_message, session_id, role, content, _now())

    async def read_session(self, session_id: str) -> Session:
        return await asyncio.to_thread(self._store.read_session, session_id)

    async def summarize(self, session_id: str, summary_text: str, covers_up_to_seq: int) -> None:
        await asyncio.to_thread(
            self._store.summarize, session_id, summary_text, covers_up_to_seq, _now()
        )

    async def trim(self, session_id: str) -> int:
        return await asyncio.to_thread(self._store.trim, session_id)

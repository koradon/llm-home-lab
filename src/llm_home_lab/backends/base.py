from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

from llm_home_lab.api.models import ChatCompletionRequest


class BackendError(Exception):
    """Base class for backend-reported failures."""


class BackendTimeoutError(BackendError):
    """The backend did not respond within its configured timeout."""


class BackendConnectionError(BackendError):
    """The backend could not be reached."""


class BackendResponseError(BackendError):
    """The backend responded with a non-2xx status."""

    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"backend responded with status {status_code}: {body}")
        self.status_code = status_code
        self.body = body


@dataclass
class BackendResponse:
    model: str
    content: str
    finish_reason: str
    prompt_tokens: int
    completion_tokens: int


@dataclass
class BackendChunk:
    content: str
    finish_reason: str | None


@dataclass
class BackendHealth:
    healthy: bool
    detail: str


class ChatBackend(Protocol):
    backend_id: str

    async def complete(self, request: ChatCompletionRequest) -> BackendResponse: ...

    def stream(self, request: ChatCompletionRequest) -> AsyncIterator[BackendChunk]: ...

    async def check_health(self) -> BackendHealth: ...

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

from orchestrator.api.models import ChatCompletionRequest


class BackendError(Exception):
    """Base class for backend-reported failures."""


class BackendTimeoutError(BackendError):
    """The backend did not respond within its configured timeout."""


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


class ChatBackend(Protocol):
    async def complete(self, request: ChatCompletionRequest) -> BackendResponse: ...

    def stream(self, request: ChatCompletionRequest) -> AsyncIterator[BackendChunk]: ...

"""Close inference responses before their owning request can release capacity."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
import httpx

from llm_home_lab.backends.base import BackendChunk


@asynccontextmanager
async def inference_response(
    client: httpx.AsyncClient, payload: dict[str, object]
) -> AsyncIterator[httpx.Response]:
    async with client.stream("POST", "/v1/chat/completions", json=payload) as response:
        try:
            yield response
        finally:
            # StreamingResponse cancellation uses an AnyIO cancel scope. Its repeated
            # cancellation must not interrupt socket/pool cleanup at an await point.
            with anyio.CancelScope(shield=True):
                await response.aclose()


@asynccontextmanager
async def closing_chunks(
    iterator: AsyncIterator[BackendChunk],
) -> AsyncIterator[AsyncIterator[BackendChunk]]:
    """Close streaming adapters explicitly, including when paused at a yield."""
    try:
        yield iterator
    finally:
        close = getattr(iterator, "aclose", None)
        if close is not None:
            with anyio.CancelScope(shield=True):
                await close()

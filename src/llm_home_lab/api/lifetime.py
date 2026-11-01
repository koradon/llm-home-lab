"""Ownership of inbound disconnects and admitted streaming responses."""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Callable

import anyio
from starlette.responses import StreamingResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class RequestLifetimeMiddleware:
    """One raw receive owner; disconnect cancels and joins the entire request.

    The pump forwards body messages with backpressure, then watches for disconnect.
    Downstream middleware/StreamingResponse receive from the mailbox, never the socket.
    This also covers queue waits and failures before a response starts iterating.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] != "/v1/chat/completions":
            await self.app(scope, receive, send)
            return

        messages: asyncio.Queue[Message] = asyncio.Queue(maxsize=1)

        async def pump() -> None:
            while True:
                message = await receive()
                await messages.put(message)
                if message["type"] == "http.disconnect":
                    return

        async def run_request() -> None:
            await self.app(scope, messages.get, send)

        request_task = asyncio.create_task(run_request())
        receive_task = asyncio.create_task(pump())
        try:
            await asyncio.wait((request_task, receive_task), return_when=asyncio.FIRST_COMPLETED)
            if request_task.done():
                await request_task
            else:
                # Surface a receive error too; finally still cancels/joins request cleanup.
                await receive_task
        finally:
            for task in (request_task, receive_task):
                if not task.done():
                    task.cancel()
            with anyio.CancelScope(shield=True):
                await asyncio.gather(request_task, receive_task, return_exceptions=True)


class SlotStream(AsyncIterator[str]):
    """Own a slot even before the underlying async generator has started."""

    def __init__(self, chunks: AsyncGenerator[str, None], release: Callable[[], None]) -> None:
        self._chunks = chunks
        self._release = release
        self._closed = False

    def __aiter__(self) -> "SlotStream":
        return self

    async def __anext__(self) -> str:
        if self._closed:
            raise StopAsyncIteration
        try:
            return await anext(self._chunks)
        except BaseException:
            await self.aclose()
            raise

    async def aclose(self) -> None:
        if self._closed:
            return
        with anyio.CancelScope(shield=True):
            try:
                await self._chunks.aclose()
            finally:
                self._closed = True
                self._release()


class SlotStreamingResponse(StreamingResponse):
    def __init__(self, chunks: SlotStream, media_type: str, headers: dict[str, str]) -> None:
        super().__init__(chunks, media_type=media_type, headers=headers)
        self._chunks = chunks

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._chunks.aclose()

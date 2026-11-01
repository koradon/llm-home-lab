"""Real API/HTTPX regression for the 2026-11-01 retained Mini slots.

All sockets are loopback and use kernel-selected ports; no runtime discovery.
"""

import asyncio
import contextlib
import json
import socket
import struct
from datetime import UTC, datetime

import httpx
import pytest
import uvicorn
from starlette.requests import ClientDisconnect

from llm_home_lab.api.app import create_app
from llm_home_lab.backends.llamaserver import LlamaCPPServerBackend
from llm_home_lab.backends.lmstudio import LMStudioBackend
from llm_home_lab.health.monitor import HealthMonitor
from llm_home_lab.observability.alerts import AlertEvaluator
from llm_home_lab.observability.metrics import MetricsRegistry
from llm_home_lab.registry.external_load import ExternalLoadStatus
from llm_home_lab.registry.models import HostCapabilities, HostCapacity
from llm_home_lab.registry.registry import HostRegistry
from llm_home_lab.routing.engine import RoutingEngine
from llm_home_lab.routing.models import PolicyRule, RoutingPolicy
from llm_home_lab.scheduling.queue import SchedulingQueue


async def eventually(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.005)


class InertProbe:
    async def probe(self, host_id, base_url, at):
        return ExternalLoadStatus(
            available=True, status="idle", queued=0, checked_at=at, total_slots=8, busy_slots=0
        )


class CountingRegistry(HostRegistry):
    def __init__(self, path):
        super().__init__(str(path))
        self.acquired = 0
        self.released = 0

    def acquire_slot(self, host_id):
        super().acquire_slot(host_id)
        self.acquired += 1

    def release_slot(self, host_id):
        super().release_slot(host_id)
        self.released += 1


class Lab:
    def __init__(self, tmp_path, mode="silent", timeout=None, backend_type="llamaserver"):
        self.registry = CountingRegistry(tmp_path / "registry.db")
        self.mode = mode
        self.timeout = timeout
        self.backend_type = backend_type
        self.accepted = 0
        self.active = 0
        self.handlers = set()
        self.clients = []
        self.queue = SchedulingQueue()
        self.holding = asyncio.Event()

    async def fake(self, reader, writer):
        task = asyncio.current_task()
        self.handlers.add(task)
        self.active += 1
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            length = next(
                (
                    int(line.split(b":", 1)[1])
                    for line in head.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                ),
                0,
            )
            await reader.readexactly(length)
            if head.startswith(b"GET /hold"):
                self.holding.set()
                await reader.read()
                return
            if not head.startswith(b"POST"):
                body = b'{"status":"ok"}'
                writer.write(
                    b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\n\r\n"
                    + body
                )
                await writer.drain()
                return
            self.accepted += 1
            if self.mode != "reset-before" and self.mode != "silent":
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                    b"Connection: close\r\n\r\n"
                )
                await writer.drain()
            if self.mode in ("reset-after", "stream", "progress"):
                count = 8 if self.mode == "progress" else 1
                for _ in range(count):
                    writer.write(
                        b'data: {"choices":[{"delta":{"content":"hello"},'
                        b'"finish_reason":null}]}\n\n'
                    )
                    await writer.drain()
                    if self.mode == "progress":
                        await asyncio.sleep(0.05)
            if self.mode.startswith("reset-"):
                writer.get_extra_info("socket").setsockopt(
                    socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
                )
                writer.transport.abort()
            elif self.mode == "progress":
                writer.write(
                    b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
                    b'"usage":{"prompt_tokens":60000,"completion_tokens":8}}\n\n'
                    b"data: [DONE]\n\n"
                )
                await writer.drain()
            else:
                # Observe EOF rather than an artificial release gate: assertions prove that
                # the orchestrator really closed every backend socket before test teardown.
                await reader.read()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
            self.active -= 1
            self.handlers.discard(task)

    async def start(self):
        self.fake_server = await asyncio.start_server(self.fake, "127.0.0.1", 0)
        fake_port = self.fake_server.sockets[0].getsockname()[1]
        assert fake_port != 8080
        backend_class = (
            LMStudioBackend if self.backend_type == "lmstudio" else LlamaCPPServerBackend
        )
        self.backend = backend_class(
            f"http://127.0.0.1:{fake_port}",
            timeout=self.timeout,
            max_retries=0,
            health_timeout=0.2,
        )
        now = datetime.now(UTC)
        self.registry.register(
            "fake",
            HostCapabilities(
                backend_type=self.backend_type,
                context_window=131072,
                base_url=f"http://127.0.0.1:{fake_port}",
                allowed_models=["gemma-4-e4b"],
            ),
            HostCapacity(max_concurrent_requests=8),
            now,
        )
        health = HealthMonitor()
        health.record_probe("fake", True, now)
        self.app = create_app(
            registry=self.registry,
            router=RoutingEngine(
                RoutingPolicy(rules=[PolicyRule(name="flat", score_fn=lambda c, ctx: 0.0)])
            ),
            health_monitor=health,
            scheduling_queue=self.queue,
            backend_factories={self.backend_type: lambda caps: self.backend},
            metrics_registry=MetricsRegistry(),
            alert_evaluator=AlertEvaluator([]),
            auth_enabled=False,
            external_load_probe=InertProbe(),
            dispatch_wait_timeout=10,
            dispatch_poll_interval=0.005,
        )
        self.server = uvicorn.Server(
            uvicorn.Config(
                self.app,
                host="127.0.0.1",
                port=0,
                log_level="critical",
                timeout_graceful_shutdown=0.1,
            )
        )
        # Uvicorn's context manager would otherwise install process-wide signal handlers.
        self.server.capture_signals = contextlib.nullcontext
        self.server_task = asyncio.create_task(self.server.serve())
        await eventually(lambda: self.server.started)
        self.port = self.server.servers[0].sockets[0].getsockname()[1]
        assert self.port != 8080

    async def request(self, stream=False):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        self.clients.append(writer)
        body = json.dumps(
            {
                "model": "gemma-4-e4b",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": stream,
            }
        ).encode()
        writer.write(
            b"POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n"
            b"Connection: close\r\nContent-Type: application/json\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\n\r\n"
            + body
        )
        await writer.drain()
        return reader, writer

    async def assert_released(self, count):
        await eventually(lambda: self.registry.in_flight("fake") == 0 and self.active == 0)
        assert self.registry.acquired == self.registry.released == count
        assert self.queue.depth() == 0
        assert not self.backend._client._transport._pool.connections

    async def close(self):
        for writer in self.clients:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
        self.server.should_exit = True
        await asyncio.wait_for(self.server_task, 3)
        await asyncio.gather(*self.server.server_state.tasks, return_exceptions=True)
        await self.backend._client.aclose()
        self.fake_server.close()
        await self.fake_server.wait_closed()
        for task in list(self.handlers):
            task.cancel()
        await asyncio.gather(*self.handlers, return_exceptions=True)


@pytest.fixture
async def lab(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_LMS_BINARY_PATH", "/nonexistent")
    labs = []

    async def make(mode="silent", timeout=None, backend_type="llamaserver"):
        instance = Lab(tmp_path, mode, timeout, backend_type)
        labs.append(instance)
        await instance.start()
        return instance

    yield make

    for instance in labs:
        await instance.close()


@pytest.mark.parametrize("backend_type", ["llamaserver", "lmstudio"])
async def test_eight_silent_exchanges_release_when_clients_close(lab, backend_type):
    instance = await lab(backend_type=backend_type)
    clients = [await instance.request() for _ in range(8)]
    await eventually(lambda: instance.accepted == 8)
    assert instance.registry.in_flight("fake") == 8
    assert (await instance.backend.check_health()).healthy

    for _, writer in clients:
        writer.close()
        await writer.wait_closed()

    await instance.assert_released(8)


async def test_eight_client_timeouts_release_silent_exchanges(lab):
    instance = await lab()
    async with httpx.AsyncClient(timeout=0.5) as client:
        tasks = [
            asyncio.create_task(
                client.post(
                    f"http://127.0.0.1:{instance.port}/v1/chat/completions",
                    json={"model": "gemma-4-e4b", "messages": [{"role": "user", "content": "Hi"}]},
                )
            )
            for _ in range(8)
        ]
        await eventually(lambda: instance.accepted == 8)
        assert instance.registry.in_flight("fake") == 8

        results = await asyncio.gather(*tasks, return_exceptions=True)

    assert all(isinstance(result, httpx.ReadTimeout) for result in results)
    await instance.assert_released(8)


@pytest.mark.parametrize("mode", ["silent", "silent-body"])
async def test_connected_silent_client_gets_504_and_releases_slot(lab, mode):
    instance = await lab(mode, timeout=0.15)
    reader, _ = await instance.request()
    await eventually(lambda: instance.accepted == 1)
    assert (await instance.backend.check_health()).healthy

    async with asyncio.timeout(3):
        response = await reader.read()

    assert response.startswith(b"HTTP/1.1 504")
    assert b'"code":"backend_timeout"' in response
    await instance.assert_released(1)


async def test_slow_progressing_response_can_exceed_gap_budget_in_total(lab):
    instance = await lab("progress", timeout=0.15)
    reader, _ = await instance.request()

    async with asyncio.timeout(3):
        response = await reader.read()

    assert response.startswith(b"HTTP/1.1 200")
    assert b"hellohellohellohellohellohellohellohello" in response
    assert b'"prompt_tokens":60000' in response
    await instance.assert_released(1)


@pytest.mark.parametrize("mode", ["reset-before", "reset-after"])
async def test_backend_reset_releases_once_before_or_after_output(lab, mode):
    instance = await lab(mode)
    reader, _ = await instance.request()

    async with asyncio.timeout(3):
        response = await reader.read()

    assert response.startswith(b"HTTP/1.1 503")
    await instance.assert_released(1)


async def test_disconnect_cancels_admitted_pool_wait(lab):
    instance = await lab()
    base_url = instance.backend._client.base_url
    await instance.backend._client.aclose()
    instance.backend._client = httpx.AsyncClient(
        base_url=base_url,
        timeout=httpx.Timeout(None, connect=0.2),
        limits=httpx.Limits(max_connections=1),
    )
    hold = asyncio.create_task(instance.backend._client.get("/hold"))
    try:
        await asyncio.wait_for(instance.holding.wait(), 3)
        _, writer = await instance.request()
        await eventually(lambda: instance.registry.in_flight("fake") == 1)

        writer.close()
        await writer.wait_closed()
        await eventually(lambda: instance.registry.in_flight("fake") == 0)

        assert instance.accepted == 0
        assert instance.registry.acquired == instance.registry.released == 1
        assert instance.active == 1  # Only the unrelated pool holder remains.
    finally:
        hold.cancel()
        await asyncio.gather(hold, return_exceptions=True)

    await instance.assert_released(1)


@pytest.mark.parametrize("mode", ["silent", "stream"])
async def test_streaming_disconnect_closes_backend_before_releasing_slot(lab, mode):
    instance = await lab(mode)
    reader, writer = await instance.request(stream=True)
    await eventually(lambda: instance.accepted == 1)
    if mode == "stream":
        async with asyncio.timeout(3):
            await reader.readuntil(b"hello")

    writer.close()
    await writer.wait_closed()

    await instance.assert_released(1)


async def test_disconnect_removes_queue_wait_without_acquiring_another_slot(lab):
    instance = await lab()
    clients = [await instance.request() for _ in range(8)]
    await eventually(lambda: instance.accepted == 8)
    _, queued_writer = await instance.request()
    await eventually(lambda: instance.queue.depth() == 1)

    queued_writer.close()
    await queued_writer.wait_closed()
    await eventually(lambda: instance.queue.depth() == 0)

    assert instance.registry.in_flight("fake") == 8
    assert instance.registry.acquired == 8
    for _, writer in clients:
        writer.close()
        await writer.wait_closed()
    await instance.assert_released(8)


@pytest.mark.parametrize("close_via_send_error", [False, True])
async def test_never_started_stream_releases_once(lab, close_via_send_error):
    from fastapi import Request, Response

    from llm_home_lab.api.models import ChatCompletionRequest

    instance = await lab()
    endpoint = next(
        route.endpoint
        for route in instance.app.routes
        if getattr(route, "path", None) == "/v1/chat/completions"
    )
    scope = {"type": "http", "headers": [], "asgi": {"spec_version": "2.4"}}
    response = await endpoint(
        ChatCompletionRequest(
            model="gemma-4-e4b", messages=[{"role": "user", "content": "Hi"}], stream=True
        ),
        Response(),
        Request(scope),
    )
    assert instance.registry.in_flight("fake") == 1

    if close_via_send_error:

        async def send(message):
            raise OSError("inbound socket closed before response start")

        async def receive():
            return {"type": "http.disconnect"}

        with pytest.raises(ClientDisconnect):
            await response(scope, receive, send)
    else:
        await response.body_iterator.aclose()
    await response.body_iterator.aclose()

    assert instance.accepted == 0
    await instance.assert_released(1)


@pytest.mark.parametrize("stream", [False, True])
async def test_slot_is_not_released_until_response_cleanup_finishes(lab, stream):
    instance = await lab()
    reading = asyncio.Event()
    closing = asyncio.Event()
    allow_close = asyncio.Event()
    close_calls = 0

    class SlowClose(httpx.AsyncByteStream):
        async def __aiter__(self):
            reading.set()
            await asyncio.Event().wait()
            yield b""  # Make this an async iterator without ever producing output.

        async def aclose(self):
            nonlocal close_calls
            close_calls += 1
            closing.set()
            await allow_close.wait()

    await instance.backend._client.aclose()
    instance.backend._client = httpx.AsyncClient(
        base_url="http://fake",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=SlowClose())),
    )
    _, writer = await instance.request(stream=stream)
    await asyncio.wait_for(reading.wait(), 3)

    try:
        writer.close()
        await writer.wait_closed()
        await asyncio.wait_for(closing.wait(), 3)
        assert instance.registry.in_flight("fake") == 1
        assert instance.registry.released == 0
    finally:
        allow_close.set()

    await eventually(lambda: instance.registry.in_flight("fake") == 0)
    assert instance.registry.acquired == instance.registry.released == close_calls == 1


async def test_connected_pool_wait_has_a_bound_independent_of_read_silence(lab):
    instance = await lab()
    # Keep the production client's finite phase timeouts, but force pool contention.
    instance.backend._client.timeout = httpx.Timeout(None, connect=0.2, pool=0.15, write=1)
    instance.backend._client._transport._pool._max_connections = 1
    hold = asyncio.create_task(instance.backend._client.get("/hold"))
    try:
        await asyncio.wait_for(instance.holding.wait(), 3)
        reader, _ = await instance.request()

        async with asyncio.timeout(3):
            response = await reader.read()

        assert response.startswith(b"HTTP/1.1 504")
        assert b'"code":"backend_timeout"' in response
        assert instance.accepted == 0
        assert instance.registry.acquired == instance.registry.released == 1
    finally:
        hold.cancel()
        await asyncio.gather(hold, return_exceptions=True)

    await instance.assert_released(1)

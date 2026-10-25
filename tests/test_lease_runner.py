import subprocess
import threading
from collections.abc import Mapping

import httpx
import pytest

from llm_home_lab.leases.models import LEASE_HEADER_NAME
from llm_home_lab.leases.runner import (
    EXIT_LOST,
    EXIT_REFUSED,
    EXIT_UNREACHABLE,
    LeaseLost,
    OrchestratorLeaseApi,
    OrchestratorUnreachable,
    ReservationRefused,
    run_leased,
)


class _FakeApi:
    def __init__(self, *, refuse: str | None = None, unreachable: bool = False) -> None:
        self.refuse = refuse
        self.unreachable = unreachable
        self.released = False
        self.heartbeats = 0
        self.reserve_kwargs: dict[str, object] | None = None
        self._lost_after = 0

    def reserve(self, **kwargs: object) -> Mapping[str, object]:
        self.reserve_kwargs = kwargs
        if self.unreachable:
            raise OrchestratorUnreachable("connection refused")
        if self.refuse is not None:
            raise ReservationRefused(self.refuse)
        return {
            "host_id": "host-a",
            "holder": kwargs["holder"],
            "token": "lease-token",
            "expires_at": "2026-10-25T10:03:00+00:00",
            "hard_expires_at": "2026-10-25T13:00:00+00:00",
        }

    def heartbeat(self, token: str) -> None:
        assert token == "lease-token"
        self.heartbeats += 1
        if self.heartbeats >= 1:
            raise LeaseLost("ceiling")

    def release(self, token: str) -> None:
        assert token == "lease-token"
        self.released = True


class _Proc:
    def __init__(self, code: int = 0, *, interrupt: bool = False, block: bool = False) -> None:
        self.code = code
        self.interrupt = interrupt
        self.block = block
        self.env: Mapping[str, str] | None = None
        self.signals: list[int] = []
        self.terminated = False
        self._done = threading.Event()
        self._waits = 0

    def wait(self, timeout: float | None = None) -> int:
        self._waits += 1
        if self.interrupt and self._waits == 1:
            raise KeyboardInterrupt
        if self.block and not self.terminated:
            assert self._done.wait(2)
        return self.code

    def terminate(self) -> None:
        self.terminated = True
        self.code = -15
        self._done.set()

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)
        self.code = -sig

    def kill(self) -> None:
        self.code = -9
        self._done.set()


def _popen_factory(proc: _Proc):
    def popen(command, env=None):
        proc.env = env
        proc.command = command
        return proc

    return popen


def test_the_wrapper_exports_the_lease_and_returns_the_command_exit_code():
    api = _FakeApi()
    api.heartbeat = lambda token: None  # type: ignore[method-assign]
    proc = _Proc(code=7)
    lines: list[str] = []

    code = run_leased(
        ["python", "-c", "pass"],
        api,
        base_url="http://orch:8080/",
        holder="ada",
        host_id="host-a",
        model=None,
        ceiling_s=None,
        heartbeat_interval_s=60,
        popen=_popen_factory(proc),
        stderr=lines.append,
    )

    assert code == 7
    assert api.released
    assert proc.env is not None
    assert proc.env["ORCHESTRATOR_BASE_URL"] == "http://orch:8080"
    assert proc.env["OPENAI_BASE_URL"] == "http://orch:8080/v1"
    assert proc.env["LLM_HOME_LAB_NODE_LEASE"] == "lease-token"
    assert proc.env["LLM_HOME_LAB_LEASE_HEADER"] == LEASE_HEADER_NAME
    assert proc.env["LLM_HOME_LAB_NODE_ID"] == "host-a"
    assert any(line.startswith("llm-home-lab-run: reserved host-a holder=ada") for line in lines)
    assert "llm-home-lab-run: released host-a" in lines


def test_a_refused_reservation_does_not_start_the_command():
    api = _FakeApi(refuse="no node is free\nnodes:\n- host-a status=online holder=bea until later")
    started = False

    def popen(command, env=None):
        nonlocal started
        started = True
        return _Proc()

    lines: list[str] = []
    code = run_leased(
        ["true"],
        api,
        base_url="http://orch",
        holder="ada",
        host_id=None,
        model="gemma",
        ceiling_s=600,
        popen=popen,
        stderr=lines.append,
    )

    assert code == EXIT_REFUSED
    assert not started
    assert not api.released
    assert any("refused:" in line and "holder=bea" in line for line in lines)
    assert api.reserve_kwargs == {
        "holder": "ada",
        "host_id": None,
        "model": "gemma",
        "ceiling_s": 600,
    }


def test_an_unreachable_orchestrator_exits_without_starting_the_command():
    api = _FakeApi(unreachable=True)
    code = run_leased(
        ["true"],
        api,
        base_url="http://orch",
        holder="ada",
        host_id=None,
        model=None,
        ceiling_s=None,
        popen=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("started")),
        stderr=lambda line: None,
    )
    assert code == EXIT_UNREACHABLE


def test_a_lost_lease_stops_the_command_and_still_releases():
    api = _FakeApi()
    proc = _Proc(block=True)
    lines: list[str] = []
    code = run_leased(
        ["sleep"],
        api,
        base_url="http://orch",
        holder="ada",
        host_id=None,
        model=None,
        ceiling_s=None,
        heartbeat_interval_s=0.01,
        popen=_popen_factory(proc),
        stderr=lines.append,
    )
    assert code == EXIT_LOST
    assert proc.terminated
    assert api.released
    assert any("lease ended:" in line for line in lines)


def test_interrupt_releases_the_lease_and_returns_the_child_code():
    api = _FakeApi()
    api.heartbeat = lambda token: None  # type: ignore[method-assign]
    proc = _Proc(interrupt=True)
    code = run_leased(
        ["sleep"],
        api,
        base_url="http://orch",
        holder="ada",
        host_id=None,
        model=None,
        ceiling_s=None,
        heartbeat_interval_s=60,
        popen=_popen_factory(proc),
        stderr=lambda line: None,
    )
    assert code == 130
    assert api.released
    assert proc.signals


def test_a_failing_command_still_releases():
    api = _FakeApi()
    api.heartbeat = lambda token: None  # type: ignore[method-assign]
    code = run_leased(
        ["false"],
        api,
        base_url="http://orch",
        holder="ada",
        host_id=None,
        model=None,
        ceiling_s=None,
        popen=_popen_factory(_Proc(code=1)),
        stderr=lambda line: None,
    )
    assert code == 1
    assert api.released


def test_the_http_client_sends_the_lease_header_and_surfaces_refusal():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["header"] = request.headers.get(LEASE_HEADER_NAME)
        if request.method == "POST" and request.url.path == "/v1/node-leases":
            return httpx.Response(
                409,
                json={
                    "error": {
                        "message": "no node is free\nnodes:\n- host-a holder=cy",
                        "code": "lease_refused",
                    }
                },
            )
        return httpx.Response(200, json={})

    api = OrchestratorLeaseApi(
        "http://orchestrator", "test-key", transport=httpx.MockTransport(handler)
    )
    try:
        api.reserve(holder="ada", host_id="host-a", model=None, ceiling_s=None)
        raise AssertionError("expected refusal")
    except ReservationRefused as exc:
        assert "holder=cy" in str(exc)
    api.close()

    def lost(request: httpx.Request) -> httpx.Response:
        assert request.headers[LEASE_HEADER_NAME] == "tok"
        return httpx.Response(
            409, json={"error": {"message": "lease has ended", "code": "lease_ended"}}
        )

    api = OrchestratorLeaseApi("http://orchestrator", None, transport=httpx.MockTransport(lost))
    try:
        api.heartbeat("tok")
        raise AssertionError("expected lease lost")
    except LeaseLost as exc:
        assert "ended" in str(exc)
    api.close()


def test_a_connection_error_is_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    api = OrchestratorLeaseApi("http://orchestrator", None, transport=httpx.MockTransport(handler))
    try:
        api.reserve(holder="ada", host_id=None, model=None, ceiling_s=None)
        raise AssertionError("expected unreachable")
    except OrchestratorUnreachable as exc:
        assert "refused" in str(exc)
    api.close()


@pytest.mark.parametrize("status", [-2, -15, -9, 0, 7])
def test_child_wait_status_is_converted_to_shell_status(status):
    api = _FakeApi()
    proc = _Proc(code=status)
    code = run_leased(
        ["command"],
        api,
        base_url="http://orch",
        holder="ada",
        host_id=None,
        model=None,
        ceiling_s=None,
        popen=_popen_factory(proc),
        stderr=lambda line: None,
    )
    assert code == (128 + abs(status) if status < 0 else status)
    assert api.released


def test_interrupt_timeout_kills_child_and_normalizes_kill_status():
    class SlowInterruptProc(_Proc):
        def wait(self, timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired("command", timeout)
            return super().wait(timeout)

    api = _FakeApi()
    proc = SlowInterruptProc(interrupt=True)
    code = run_leased(
        ["command"],
        api,
        base_url="http://orch",
        holder="ada",
        host_id=None,
        model=None,
        ceiling_s=None,
        popen=_popen_factory(proc),
        stderr=lambda line: None,
    )
    assert proc.code == -9
    assert code == 137
    assert api.released

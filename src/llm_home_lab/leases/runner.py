"""Reserve a node, run a command against the orchestrator, and always release it."""

import argparse
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Protocol

import httpx

from llm_home_lab.leases.models import LEASE_HEADER_NAME

# The wrapped command's exit code is returned unchanged when it runs to completion.
# These three are used only for the wrapper itself.
EXIT_REFUSED = 2
EXIT_UNREACHABLE = 3
EXIT_LOST = 4

HEARTBEAT_INTERVAL_S = 60.0
_JOIN_TIMEOUT_S = 2.0


class ReservationRefused(Exception):
    """The orchestrator refused the reservation. Exit 2."""


class OrchestratorUnreachable(Exception):
    """The orchestrator could not be contacted. Exit 3."""


class LeaseLost(Exception):
    """The lease ended while the command was still running. Exit 4."""


class LeaseApi(Protocol):
    def reserve(
        self,
        *,
        holder: str,
        host_id: str | None,
        model: str | None,
        ceiling_s: int | None,
    ) -> Mapping[str, object]: ...

    def heartbeat(self, token: str) -> None: ...

    def release(self, token: str) -> None: ...


class OrchestratorLeaseApi:
    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        *,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers=headers,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def reserve(
        self,
        *,
        holder: str,
        host_id: str | None,
        model: str | None,
        ceiling_s: int | None,
    ) -> Mapping[str, object]:
        body: dict[str, object] = {"holder": holder}
        if host_id is not None:
            body["host_id"] = host_id
        if model is not None:
            body["model"] = model
        if ceiling_s is not None:
            body["ceiling_s"] = ceiling_s
        response = self._request("POST", "/v1/node-leases", json_body=body)
        if response.status_code in (400, 404, 409):
            raise ReservationRefused(_error_message(response))
        if response.status_code >= 400:
            raise OrchestratorUnreachable(_error_message(response))
        payload = response.json()
        if not isinstance(payload, dict):
            raise OrchestratorUnreachable("lease response was not an object")
        return payload

    def heartbeat(self, token: str) -> None:
        response = self._request(
            "POST",
            "/v1/node-leases/heartbeat",
            headers={LEASE_HEADER_NAME: token},
        )
        if response.status_code == 409:
            raise LeaseLost(_error_message(response))
        if response.status_code == 403:
            raise LeaseLost(_error_message(response))
        if response.status_code >= 400:
            raise OrchestratorUnreachable(_error_message(response))

    def release(self, token: str) -> None:
        response = self._request(
            "DELETE",
            "/v1/node-leases",
            headers={LEASE_HEADER_NAME: token},
        )
        if response.status_code == 404:
            return
        if response.status_code >= 400:
            raise OrchestratorUnreachable(_error_message(response))

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        try:
            return self._client.request(method, path, json=json_body, headers=headers)
        except httpx.TransportError as exc:
            raise OrchestratorUnreachable(str(exc) or type(exc).__name__) from exc


def child_environment(base_url: str, lease: Mapping[str, object]) -> dict[str, str]:
    root = base_url.rstrip("/")
    token = str(lease["token"])
    host_id = str(lease["host_id"])
    env = os.environ.copy()
    env["ORCHESTRATOR_BASE_URL"] = root
    env["OPENAI_BASE_URL"] = f"{root}/v1"
    env["LLM_HOME_LAB_NODE_LEASE"] = token
    env["LLM_HOME_LAB_LEASE_HEADER"] = LEASE_HEADER_NAME
    env["LLM_HOME_LAB_NODE_ID"] = host_id
    return env


def run_leased(
    command: Sequence[str],
    api: LeaseApi,
    *,
    base_url: str,
    holder: str,
    host_id: str | None,
    model: str | None,
    ceiling_s: int | None,
    heartbeat_interval_s: float = HEARTBEAT_INTERVAL_S,
    popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    stderr: Callable[[str], None] | None = None,
) -> int:
    """Reserve, run ``command``, heartbeat, and release on every exit path."""
    log = stderr or (lambda line: print(line, file=sys.stderr))
    if not command:
        log("llm-home-lab-run: error: missing command (pass it after --)")
        return EXIT_REFUSED

    try:
        lease = api.reserve(holder=holder, host_id=host_id, model=model, ceiling_s=ceiling_s)
    except ReservationRefused as exc:
        log(f"llm-home-lab-run: refused: {exc}")
        return EXIT_REFUSED
    except OrchestratorUnreachable as exc:
        log(f"llm-home-lab-run: unreachable: {exc}")
        return EXIT_UNREACHABLE

    token = str(lease["token"])
    leased_host = str(lease["host_id"])
    log(
        "llm-home-lab-run: reserved "
        f"{leased_host} holder={lease['holder']} until {lease['expires_at']} "
        f"ceiling {lease['hard_expires_at']}"
    )
    env = child_environment(base_url, lease)
    try:
        proc = popen(list(command), env=env)
    except OSError as exc:
        log(f"llm-home-lab-run: error: {exc}")
        _release(api, token, leased_host, log)
        return 127

    lost = False
    stop = threading.Event()

    def beat() -> None:
        nonlocal lost
        while not stop.wait(heartbeat_interval_s):
            try:
                api.heartbeat(token)
            except LeaseLost as exc:
                lost = True
                log(f"llm-home-lab-run: lease ended: {exc}")
                proc.terminate()
                return
            except OrchestratorUnreachable as exc:
                log(f"llm-home-lab-run: heartbeat failed: {exc}")

    beater = threading.Thread(target=beat, name="llm-home-lab-lease-heartbeat", daemon=True)
    beater.start()
    try:
        code = _wait_for(proc)
    finally:
        stop.set()
        beater.join(timeout=_JOIN_TIMEOUT_S)
        _release(api, token, leased_host, log)
    if lost:
        return EXIT_LOST
    return code


def _release(api: LeaseApi, token: str, host_id: str, log: Callable[[str], None]) -> None:
    try:
        api.release(token)
        log(f"llm-home-lab-run: released {host_id}")
    except OrchestratorUnreachable as exc:
        log(f"llm-home-lab-run: release failed: {exc}")


def _wait_for(proc: subprocess.Popen[bytes]) -> int:
    try:
        return proc.wait()
    except KeyboardInterrupt:
        proc.send_signal(signal.SIGINT)
        try:
            return proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            return proc.wait()


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text or f"HTTP {response.status_code}"
    if isinstance(body, dict):
        error = body.get("error")
        message = error.get("message") if isinstance(error, dict) else None
        if isinstance(message, str):
            return message
    return response.text or f"HTTP {response.status_code}"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llm-home-lab-run",
        description=(
            "Reserve one orchestrator node, run a command with lease environment "
            "variables set, heartbeat the lease, and release it when the command ends."
        ),
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("ORCHESTRATOR_BASE_URL", "http://localhost:8080"),
        help="orchestrator origin (default: ORCHESTRATOR_BASE_URL or http://localhost:8080)",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("ORCHESTRATOR_API_KEY"),
        help="orchestrator API key (default: ORCHESTRATOR_API_KEY)",
    )
    parser.add_argument(
        "--holder",
        default=os.environ.get("LLM_HOME_LAB_HOLDER") or f"pid-{os.getpid()}",
        help="holder id recorded on the lease (default: LLM_HOME_LAB_HOLDER or pid-<pid>)",
    )
    parser.add_argument("--node", default=None, help="reserve this host_id instead of picking one")
    parser.add_argument("--model", default=None, help="reserve a node that serves this model")
    parser.add_argument(
        "--ceiling-seconds",
        type=int,
        default=None,
        help="total hold ceiling for this reservation, up to the server maximum",
    )
    parser.add_argument(
        "--heartbeat-seconds",
        type=float,
        default=HEARTBEAT_INTERVAL_S,
        help=f"how often to renew the lease (default: {HEARTBEAT_INTERVAL_S:g})",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="command to run; put -- before it when it has its own flags",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    api = OrchestratorLeaseApi(args.base_url, args.api_key)
    try:
        return run_leased(
            command,
            api,
            base_url=args.base_url,
            holder=args.holder,
            host_id=args.node,
            model=args.model,
            ceiling_s=args.ceiling_seconds,
            heartbeat_interval_s=args.heartbeat_seconds,
        )
    finally:
        api.close()


if __name__ == "__main__":
    raise SystemExit(main())

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


@dataclass
class ExternalLoadStatus:
    available: bool
    status: str | None
    queued: int | None
    checked_at: datetime


def _unavailable(at: datetime) -> ExternalLoadStatus:
    return ExternalLoadStatus(available=False, status=None, queued=None, checked_at=at)


class ExternalLoadProbe:
    def __init__(
        self,
        lms_binary: str = "lms",
        timeout_s: float = 5.0,
        cache_ttl: timedelta = timedelta(seconds=2),
        create_subprocess: Callable[..., Awaitable[asyncio.subprocess.Process]] | None = None,
    ) -> None:
        self._lms_binary = lms_binary
        self._timeout_s = timeout_s
        self._cache_ttl = cache_ttl
        self._create_subprocess = create_subprocess or asyncio.create_subprocess_exec
        self._cache: dict[str, tuple[datetime, ExternalLoadStatus]] = {}

    @property
    def lms_binary(self) -> str:
        return self._lms_binary

    @property
    def cache_ttl(self) -> timedelta:
        return self._cache_ttl

    async def probe(self, host_id: str, base_url: str, at: datetime) -> ExternalLoadStatus:
        cached = self._cache.get(host_id)
        if cached is not None and at - cached[0] < self._cache_ttl:
            return cached[1]

        status = await self._probe_uncached(base_url, at)
        self._cache[host_id] = (at, status)
        return status

    async def _probe_uncached(self, base_url: str, at: datetime) -> ExternalLoadStatus:
        hostname = urlparse(base_url).hostname
        if not hostname:
            return _unavailable(at)

        try:
            proc = await self._create_subprocess(
                self._lms_binary,
                "ps",
                "--host",
                hostname,
                "--json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            logger.info("lms binary %r not found; external load unavailable", self._lms_binary)
            return _unavailable(at)

        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=self._timeout_s)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            logger.info("lms ps --host %s timed out; external load unavailable", hostname)
            return _unavailable(at)

        if proc.returncode != 0:
            logger.info(
                "lms ps --host %s exited %s; external load unavailable", hostname, proc.returncode
            )
            return _unavailable(at)

        try:
            entries = json.loads(stdout)
        except json.JSONDecodeError:
            logger.info(
                "lms ps --host %s returned unparseable output; external load unavailable", hostname
            )
            return _unavailable(at)

        return _summarize(entries, at)


def _summarize(entries: list[dict[str, object]], at: datetime) -> ExternalLoadStatus:
    if not entries:
        return ExternalLoadStatus(available=True, status="idle", queued=0, checked_at=at)

    total_queued = 0
    status = "idle"
    for entry in entries:
        queued_value = entry.get("queued", 0)
        total_queued += queued_value if isinstance(queued_value, int) else 0
        entry_status = entry.get("status", "idle")
        if entry_status != "idle" and status == "idle":
            status = str(entry_status)

    return ExternalLoadStatus(available=True, status=status, queued=total_queued, checked_at=at)

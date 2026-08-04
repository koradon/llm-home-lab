import logging
from datetime import datetime, timedelta

import httpx

from llm_home_lab.registry.external_load import ExternalLoadStatus

logger = logging.getLogger(__name__)


def _unavailable(at: datetime) -> ExternalLoadStatus:
    return ExternalLoadStatus(available=False, status=None, queued=None, checked_at=at)


class LlamaCPPServerLoadProbe:
    def __init__(
        self,
        timeout_s: float = 5.0,
        cache_ttl: timedelta = timedelta(seconds=2),
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._timeout_s = timeout_s
        self._cache_ttl = cache_ttl
        self._client = httpx.AsyncClient(timeout=timeout_s, transport=transport)
        self._cache: dict[str, tuple[datetime, ExternalLoadStatus]] = {}

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
        try:
            response = await self._client.get(f"{base_url}/slots")
        except httpx.TransportError as exc:
            logger.info("GET %s/slots failed (%s); external load unavailable", base_url, exc)
            return _unavailable(at)

        if response.status_code == 404:
            # /slots is on by default but an operator can disable it with a flag — that's a
            # missing signal, not an unhealthy node, so this degrades the same as "unavailable".
            logger.info("GET %s/slots returned 404 (disabled); external load unavailable", base_url)
            return _unavailable(at)

        if response.status_code // 100 != 2:
            logger.info(
                "GET %s/slots returned %s; external load unavailable",
                base_url,
                response.status_code,
            )
            return _unavailable(at)

        try:
            slots = response.json()
        except ValueError:
            logger.info(
                "GET %s/slots returned unparseable output; external load unavailable", base_url
            )
            return _unavailable(at)

        return _summarize(slots, at)


def _summarize(slots: list[dict[str, object]], at: datetime) -> ExternalLoadStatus:
    if not slots:
        return ExternalLoadStatus(available=True, status="idle", queued=0, checked_at=at)

    # llama-server has a fixed pool of slots and no native queue-depth metric — the count of
    # slots currently generating is the closest available proxy for "how backed up is this host."
    busy = sum(1 for slot in slots if slot.get("is_processing"))
    status = "busy" if busy else "idle"
    return ExternalLoadStatus(available=True, status=status, queued=busy, checked_at=at)

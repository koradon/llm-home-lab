import logging
from collections.abc import Sequence
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
        self._cache: dict[tuple[str, tuple[str, ...]], tuple[datetime, ExternalLoadStatus]] = {}

    @property
    def cache_ttl(self) -> timedelta:
        return self._cache_ttl

    async def probe(
        self, host_id: str, base_url: str, at: datetime, *, model: str | None = None
    ) -> ExternalLoadStatus:
        key = (host_id, (model,) if model is not None else ())
        cached = self._cache.get(key)
        if cached is not None and at - cached[0] < self._cache_ttl:
            return cached[1]

        status = await self._probe_uncached(base_url, at, model=model)
        self._cache[key] = (at, status)
        return status

    async def probe_models(
        self, host_id: str, base_url: str, models: Sequence[str], at: datetime
    ) -> ExternalLoadStatus:
        if not models:
            return _unavailable(at)

        model_key = tuple(models)
        cache_key = (host_id, model_key)
        cached = self._cache.get(cache_key)
        if cached is not None and at - cached[0] < self._cache_ttl:
            return cached[1]

        if len(model_key) == 1:
            status = await self._probe_uncached(base_url, at, model=model_key[0])
        else:
            results = [
                await self._probe_uncached(base_url, at, model=model_name)
                for model_name in model_key
            ]
            available = [item for item in results if item.available]
            if not available:
                status = _unavailable(at)
            else:
                total_slots = sum(item.total_slots or 0 for item in available)
                busy_slots = sum(item.busy_slots or 0 for item in available)
                status = ExternalLoadStatus(
                    available=True,
                    status="busy" if busy_slots else "idle",
                    queued=busy_slots,
                    checked_at=at,
                    total_slots=total_slots,
                    busy_slots=busy_slots,
                )

        self._cache[cache_key] = (at, status)
        return status

    async def _probe_uncached(
        self, base_url: str, at: datetime, *, model: str | None = None
    ) -> ExternalLoadStatus:
        url = f"{base_url.rstrip('/')}/slots"
        params = {"model": model} if model is not None else None
        try:
            response = await self._client.get(url, params=params)
        except httpx.TransportError as exc:
            logger.info("GET %s failed (%s); external load unavailable", url, exc)
            return _unavailable(at)

        if response.status_code == 404:
            # /slots is on by default but an operator can disable it with a flag — that's a
            # missing signal, not an unhealthy node, so this degrades the same as "unavailable".
            logger.info("GET %s returned 404 (disabled); external load unavailable", url)
            return _unavailable(at)

        if response.status_code // 100 != 2:
            logger.info(
                "GET %s returned %s; external load unavailable",
                url,
                response.status_code,
            )
            return _unavailable(at)

        try:
            slots = response.json()
        except ValueError:
            logger.info("GET %s returned unparseable output; external load unavailable", url)
            return _unavailable(at)

        if not isinstance(slots, list):
            logger.info("GET %s returned non-list payload; external load unavailable", url)
            return _unavailable(at)

        return _summarize(slots, at)


def _summarize(slots: list[dict[str, object]], at: datetime) -> ExternalLoadStatus:
    if not slots:
        return ExternalLoadStatus(
            available=True, status="idle", queued=0, checked_at=at, total_slots=0, busy_slots=0
        )

    # llama-server has a fixed pool of slots and no native queue-depth metric — `queued` keeps the
    # busy-slot count for backward compatibility, but it is not a backlog: the TUI shows
    # `busy_slots`/`total_slots` instead.
    busy = sum(1 for slot in slots if slot.get("is_processing"))
    status = "busy" if busy else "idle"
    return ExternalLoadStatus(
        available=True,
        status=status,
        queued=busy,
        checked_at=at,
        total_slots=len(slots),
        busy_slots=busy,
    )

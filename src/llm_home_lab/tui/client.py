from typing import Literal, cast

import httpx


class DiagnosticsClientError(Exception):
    def __init__(
        self, kind: Literal["connection", "unauthorized", "server_error"], detail: str
    ) -> None:
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


class OrchestratorDiagnosticsClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    async def list_nodes(self) -> dict[str, object]:
        response = await self._get("/v1/nodes")
        return cast(dict[str, object], response.json())

    async def list_alerts(self) -> dict[str, object]:
        response = await self._get("/v1/alerts")
        return cast(dict[str, object], response.json())

    async def fetch_metrics_text(self) -> str:
        response = await self._get("/metrics")
        return response.text

    async def trigger_health_check(self) -> None:
        # GET /v1/nodes reports a host's status from HealthMonitor's probe history, but nothing
        # populates that history unless something calls /health/ready — that endpoint is the only
        # place a probe gets recorded (see ADR/plan notes on failover-and-health-policy). Without
        # a caller, every node stays "unknown" forever. The dashboard is exactly the kind of
        # external prober that design expects, so it drives the cadence itself. A 503 here is a
        # normal "some backend is unhealthy" outcome, not an error — only a transport failure is.
        try:
            await self._client.get("/health/ready")
        except httpx.TransportError as exc:
            raise DiagnosticsClientError("connection", str(exc)) from exc

    async def _get(self, path: str) -> httpx.Response:
        try:
            response = await self._client.get(path)
        except httpx.TransportError as exc:
            raise DiagnosticsClientError("connection", str(exc)) from exc

        if response.status_code in (401, 403):
            raise DiagnosticsClientError("unauthorized", f"status {response.status_code}")
        if response.status_code // 100 != 2:
            raise DiagnosticsClientError("server_error", f"status {response.status_code}")
        return response

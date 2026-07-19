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

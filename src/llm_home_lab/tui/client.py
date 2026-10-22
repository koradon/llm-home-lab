from typing import Literal, cast

import httpx


class DiagnosticsClientError(Exception):
    def __init__(
        self, kind: Literal["connection", "timeout", "unauthorized", "server_error"], detail: str
    ) -> None:
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


def _transport_error(exc: httpx.TransportError) -> DiagnosticsClientError:
    # A timeout means the orchestrator accepted the connection but answered too slowly — it is
    # up, so it must not be reported as unreachable.
    kind: Literal["connection", "timeout"] = (
        "timeout" if isinstance(exc, httpx.TimeoutException) else "connection"
    )
    return DiagnosticsClientError(kind, str(exc) or type(exc).__name__)


class OrchestratorDiagnosticsClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 15.0,
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

    async def register_node(self, fields: dict[str, object]) -> None:
        await self._post("/v1/nodes/register", fields)

    async def update_node(self, host_id: str, fields: dict[str, object]) -> None:
        await self._patch(f"/v1/nodes/{host_id}", fields)

    async def deregister_node(self, host_id: str) -> None:
        await self._delete(f"/v1/nodes/{host_id}")

    async def _get(self, path: str) -> httpx.Response:
        try:
            response = await self._client.get(path)
        except httpx.TransportError as exc:
            raise _transport_error(exc) from exc
        return self._raise_for_error_status(response)

    async def _post(self, path: str, json: dict[str, object]) -> httpx.Response:
        try:
            response = await self._client.post(path, json=json)
        except httpx.TransportError as exc:
            raise _transport_error(exc) from exc
        return self._raise_for_error_status(response)

    async def _patch(self, path: str, json: dict[str, object]) -> httpx.Response:
        try:
            response = await self._client.patch(path, json=json)
        except httpx.TransportError as exc:
            raise _transport_error(exc) from exc
        return self._raise_for_error_status(response)

    async def _delete(self, path: str) -> httpx.Response:
        try:
            response = await self._client.delete(path)
        except httpx.TransportError as exc:
            raise _transport_error(exc) from exc
        return self._raise_for_error_status(response)

    def _raise_for_error_status(self, response: httpx.Response) -> httpx.Response:
        if response.status_code in (401, 403):
            raise DiagnosticsClientError("unauthorized", f"status {response.status_code}")
        if response.status_code // 100 != 2:
            raise DiagnosticsClientError("server_error", f"status {response.status_code}")
        return response

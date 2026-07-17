from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass
class ApiKey:
    key: str
    expires_at: datetime | None


@dataclass
class ClientConfig:
    client_id: str
    allowed_path_prefixes: list[str]
    keys: list[ApiKey]


@dataclass
class ClientIdentity:
    client_id: str
    allowed_path_prefixes: list[str]


def api_key_to_dict(api_key: ApiKey) -> dict[str, object]:
    return {
        "key": api_key.key,
        "expires_at": api_key.expires_at.isoformat() if api_key.expires_at else None,
    }


def api_key_from_dict(data: dict[str, Any]) -> ApiKey:
    expires_at = data.get("expires_at")
    return ApiKey(
        key=str(data["key"]),
        expires_at=datetime.fromisoformat(str(expires_at)) if expires_at else None,
    )


def client_config_to_dict(client: ClientConfig) -> dict[str, object]:
    return {
        "client_id": client.client_id,
        "allowed_path_prefixes": client.allowed_path_prefixes,
        "keys": [api_key_to_dict(key) for key in client.keys],
    }


def client_config_from_dict(data: dict[str, Any]) -> ClientConfig:
    return ClientConfig(
        client_id=str(data["client_id"]),
        allowed_path_prefixes=list(data["allowed_path_prefixes"]),
        keys=[api_key_from_dict(key) for key in data["keys"]],
    )


__all__ = [
    "ApiKey",
    "ClientConfig",
    "ClientIdentity",
    "api_key_from_dict",
    "api_key_to_dict",
    "client_config_from_dict",
    "client_config_to_dict",
]

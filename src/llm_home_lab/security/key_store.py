import json
from collections.abc import Sequence
from datetime import datetime

from llm_home_lab.security.models import ClientConfig, ClientIdentity, client_config_from_dict


class ApiKeyStore:
    def __init__(self, clients: Sequence[ClientConfig]) -> None:
        self._clients = list(clients)

    @classmethod
    def from_file(cls, path: str) -> "ApiKeyStore":
        with open(path) as f:
            data = json.load(f)
        return cls([client_config_from_dict(client) for client in data["clients"]])

    def authenticate(self, bearer_token: str, at: datetime) -> ClientIdentity | None:
        for client in self._clients:
            for api_key in client.keys:
                if api_key.key != bearer_token:
                    continue
                if api_key.expires_at is not None and api_key.expires_at <= at:
                    continue
                return ClientIdentity(
                    client_id=client.client_id,
                    allowed_path_prefixes=client.allowed_path_prefixes,
                )
        return None

    def is_authorized(self, identity: ClientIdentity, path: str) -> bool:
        return any(path.startswith(prefix) for prefix in identity.allowed_path_prefixes)

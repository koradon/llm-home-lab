import argparse
import json
import os
import secrets
import sys
from datetime import UTC, datetime, timedelta

from llm_home_lab.security.models import (
    ApiKey,
    ClientConfig,
    client_config_from_dict,
    client_config_to_dict,
)

DEFAULT_GRACE_PERIOD_HOURS = 24.0


class ClientNotFoundError(Exception):
    """No configured client matches the given client_id."""


def load_clients(path: str) -> list[ClientConfig]:
    with open(path) as f:
        data = json.load(f)
    return [client_config_from_dict(client) for client in data["clients"]]


def save_clients(path: str, clients: list[ClientConfig]) -> None:
    data = {"clients": [client_config_to_dict(client) for client in clients]}
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def rotate_client_key(
    clients: list[ClientConfig],
    client_id: str,
    new_key: str,
    now: datetime,
    grace_period: timedelta,
) -> list[ClientConfig]:
    found = False
    updated: list[ClientConfig] = []
    for client in clients:
        if client.client_id != client_id:
            updated.append(client)
            continue
        found = True
        expiring_keys = [
            ApiKey(key=k.key, expires_at=now + grace_period) if k.expires_at is None else k
            for k in client.keys
        ]
        updated.append(
            ClientConfig(
                client_id=client.client_id,
                allowed_path_prefixes=client.allowed_path_prefixes,
                keys=[*expiring_keys, ApiKey(key=new_key, expires_at=None)],
            )
        )
    if not found:
        raise ClientNotFoundError(client_id)
    return updated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rotate an orchestrator client's API key.")
    parser.add_argument("client_id")
    parser.add_argument("--grace-period-hours", type=float, default=DEFAULT_GRACE_PERIOD_HOURS)
    parser.add_argument(
        "--keys-file",
        default=os.environ.get("ORCHESTRATOR_API_KEYS_FILE", "./config/api_keys.json"),
    )
    args = parser.parse_args(argv)

    clients = load_clients(args.keys_file)
    new_key = secrets.token_urlsafe(32)
    try:
        updated = rotate_client_key(
            clients,
            args.client_id,
            new_key,
            datetime.now(UTC),
            timedelta(hours=args.grace_period_hours),
        )
    except ClientNotFoundError:
        print(f"error: client_id {args.client_id!r} not found in {args.keys_file}", file=sys.stderr)
        return 1

    save_clients(args.keys_file, updated)
    print(new_key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

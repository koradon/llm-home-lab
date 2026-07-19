import json
from datetime import UTC, datetime, timedelta

from llm_home_lab.security.key_store import ApiKeyStore
from llm_home_lab.security.models import ApiKey, ClientConfig

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _client(
    client_id: str = "chat-client",
    keys: list[ApiKey] | None = None,
    allowed_path_prefixes: list[str] | None = None,
) -> ClientConfig:
    return ClientConfig(
        client_id=client_id,
        allowed_path_prefixes=allowed_path_prefixes or ["/v1/chat/completions"],
        keys=keys or [ApiKey(key="sk-abc", expires_at=None)],
    )


def test_authenticate_resolves_a_valid_token_to_its_client_identity():
    store = ApiKeyStore([_client("chat-client", keys=[ApiKey(key="sk-abc", expires_at=None)])])

    identity = store.authenticate("sk-abc", at=T0)

    assert identity is not None
    assert identity.client_id == "chat-client"


def test_authenticate_returns_none_for_an_unrecognized_token():
    store = ApiKeyStore([_client("chat-client", keys=[ApiKey(key="sk-abc", expires_at=None)])])

    identity = store.authenticate("sk-does-not-exist", at=T0)

    assert identity is None


def test_authenticate_returns_none_for_an_expired_token():
    store = ApiKeyStore(
        [
            _client(
                "chat-client",
                keys=[ApiKey(key="sk-old", expires_at=T0 - timedelta(seconds=1))],
            )
        ]
    )

    identity = store.authenticate("sk-old", at=T0)

    assert identity is None


def test_authenticate_accepts_a_key_whose_expiry_is_still_in_the_future():
    store = ApiKeyStore(
        [
            _client(
                "chat-client",
                keys=[ApiKey(key="sk-fresh", expires_at=T0 + timedelta(seconds=1))],
            )
        ]
    )

    identity = store.authenticate("sk-fresh", at=T0)

    assert identity is not None


def test_is_authorized_true_when_path_matches_an_allowed_prefix():
    store = ApiKeyStore([_client(allowed_path_prefixes=["/v1/chat/completions"])])
    identity = store.authenticate("sk-abc", at=T0)

    assert store.is_authorized(identity, "/v1/chat/completions") is True


def test_is_authorized_false_when_path_matches_no_allowed_prefix():
    store = ApiKeyStore([_client(allowed_path_prefixes=["/v1/chat/completions"])])
    identity = store.authenticate("sk-abc", at=T0)

    assert store.is_authorized(identity, "/v1/nodes") is False


def test_two_valid_keys_for_the_same_client_both_authenticate_during_a_rotation_window():
    store = ApiKeyStore(
        [
            _client(
                "chat-client",
                keys=[
                    ApiKey(key="sk-old", expires_at=T0 + timedelta(hours=24)),
                    ApiKey(key="sk-new", expires_at=None),
                ],
            )
        ]
    )

    old_identity = store.authenticate("sk-old", at=T0)
    new_identity = store.authenticate("sk-new", at=T0)

    assert old_identity is not None and old_identity.client_id == "chat-client"
    assert new_identity is not None and new_identity.client_id == "chat-client"


def test_from_file_loads_clients_and_keys_from_json(tmp_path):
    keys_file = tmp_path / "api_keys.json"
    keys_file.write_text(
        json.dumps(
            {
                "clients": [
                    {
                        "client_id": "chat-client",
                        "allowed_path_prefixes": ["/v1/chat/completions"],
                        "keys": [{"key": "sk-abc", "expires_at": None}],
                    }
                ]
            }
        )
    )

    store = ApiKeyStore.from_file(str(keys_file))

    identity = store.authenticate("sk-abc", at=T0)
    assert identity is not None
    assert identity.client_id == "chat-client"


def test_from_file_parses_an_expires_at_timestamp(tmp_path):
    keys_file = tmp_path / "api_keys.json"
    keys_file.write_text(
        json.dumps(
            {
                "clients": [
                    {
                        "client_id": "chat-client",
                        "allowed_path_prefixes": ["/v1/chat/completions"],
                        "keys": [
                            {
                                "key": "sk-old",
                                "expires_at": (T0 - timedelta(seconds=1)).isoformat(),
                            }
                        ],
                    }
                ]
            }
        )
    )

    store = ApiKeyStore.from_file(str(keys_file))

    assert store.authenticate("sk-old", at=T0) is None

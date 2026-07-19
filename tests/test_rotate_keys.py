import json
from datetime import UTC, datetime, timedelta

import pytest

from llm_home_lab.security.models import ApiKey, ClientConfig
from llm_home_lab.security.rotate_keys import (
    ClientNotFoundError,
    load_clients,
    main,
    rotate_client_key,
    save_clients,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _client(client_id="chat-client", keys=None) -> ClientConfig:
    return ClientConfig(
        client_id=client_id,
        allowed_path_prefixes=["/v1/chat/completions"],
        keys=keys or [ApiKey(key="sk-old", expires_at=None)],
    )


def test_rotate_appends_a_new_non_expiring_key():
    clients = [_client(keys=[ApiKey(key="sk-old", expires_at=None)])]

    updated = rotate_client_key(clients, "chat-client", "sk-new", T0, timedelta(hours=24))

    rotated = next(c for c in updated if c.client_id == "chat-client")
    assert any(k.key == "sk-new" and k.expires_at is None for k in rotated.keys)


def test_rotate_stamps_the_old_non_expiring_key_with_the_grace_period():
    clients = [_client(keys=[ApiKey(key="sk-old", expires_at=None)])]

    updated = rotate_client_key(clients, "chat-client", "sk-new", T0, timedelta(hours=24))

    rotated = next(c for c in updated if c.client_id == "chat-client")
    old_key = next(k for k in rotated.keys if k.key == "sk-old")
    assert old_key.expires_at == T0 + timedelta(hours=24)


def test_rotate_leaves_an_already_expiring_key_untouched():
    already_expiring = T0 + timedelta(hours=1)
    clients = [_client(keys=[ApiKey(key="sk-old", expires_at=already_expiring)])]

    updated = rotate_client_key(clients, "chat-client", "sk-new", T0, timedelta(hours=24))

    rotated = next(c for c in updated if c.client_id == "chat-client")
    old_key = next(k for k in rotated.keys if k.key == "sk-old")
    assert old_key.expires_at == already_expiring


def test_rotate_does_not_touch_other_clients():
    other = _client("node-operator", keys=[ApiKey(key="sk-node", expires_at=None)])
    clients = [_client("chat-client"), other]

    updated = rotate_client_key(clients, "chat-client", "sk-new", T0, timedelta(hours=24))

    untouched = next(c for c in updated if c.client_id == "node-operator")
    assert untouched.keys == [ApiKey(key="sk-node", expires_at=None)]


def test_rotate_raises_for_an_unknown_client_id():
    clients = [_client("chat-client")]

    with pytest.raises(ClientNotFoundError):
        rotate_client_key(clients, "does-not-exist", "sk-new", T0, timedelta(hours=24))


def test_save_and_load_clients_round_trip_through_a_file(tmp_path):
    keys_file = tmp_path / "api_keys.json"
    clients = [_client("chat-client", keys=[ApiKey(key="sk-abc", expires_at=None)])]

    save_clients(str(keys_file), clients)
    loaded = load_clients(str(keys_file))

    assert loaded == clients
    assert json.loads(keys_file.read_text())["clients"][0]["client_id"] == "chat-client"


def test_main_prints_the_new_key_and_updates_the_file(tmp_path, capsys):
    keys_file = tmp_path / "api_keys.json"
    save_clients(
        str(keys_file), [_client("chat-client", keys=[ApiKey(key="sk-old", expires_at=None)])]
    )

    exit_code = main(["chat-client", "--keys-file", str(keys_file)])

    assert exit_code == 0
    printed_key = capsys.readouterr().out.strip()
    assert printed_key
    updated = load_clients(str(keys_file))
    assert any(k.key == printed_key for k in updated[0].keys)


def test_main_exits_non_zero_for_an_unknown_client(tmp_path, capsys):
    keys_file = tmp_path / "api_keys.json"
    save_clients(str(keys_file), [_client("chat-client")])

    exit_code = main(["does-not-exist", "--keys-file", str(keys_file)])

    assert exit_code == 1
    assert "does-not-exist" in capsys.readouterr().err

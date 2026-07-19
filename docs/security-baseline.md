# Security Baseline

How authentication, authorization, secrets, and audit logging work in this orchestrator. See
[the spec](specs/security-and-governance-baseline.md) for the full behavioral contract; this doc
is the practical how-to for operators and contributors.

## Authentication

Every HTTP request except `GET /health/live` and `GET /health/ready` must carry
`Authorization: Bearer <key>`. The orchestrator loads clients and their keys once at startup from
a JSON file (`ApiKeyStore.from_file`, `src/llm_home_lab/security/key_store.py`), whose path comes
from the `ORCHESTRATOR_API_KEYS_FILE` env var (default `./config/api_keys.json`). If that file
doesn't exist, the orchestrator starts anyway with zero configured clients — every non-exempt
request is `401` until you create the file.

**This file is never picked up without a restart.** Editing it (by hand or via the rotation
script below) has no effect until the orchestrator process restarts, exactly like every other
env-var-driven config value in this codebase (`LMSTUDIO_BASE_URL`, etc.).

## Adding a client

Edit (or create) the keys file:

```json
{
  "clients": [
    {
      "client_id": "chat-client",
      "allowed_path_prefixes": ["/v1/chat/completions"],
      "keys": [{"key": "sk-...", "expires_at": null}]
    },
    {
      "client_id": "node-operator",
      "allowed_path_prefixes": ["/v1/nodes"],
      "keys": [{"key": "sk-...", "expires_at": null}]
    }
  ]
}
```

- `allowed_path_prefixes` is a list of path prefixes this client may reach — matched with a plain
  `path.startswith(prefix)`, not per-HTTP-method. A client scoped to `/v1/nodes` can hit every
  `/v1/nodes/*` endpoint (register, heartbeat, deregister, list).
- `expires_at` is `null` for a key with no planned expiry, or an ISO-8601 timestamp string. A key
  past its `expires_at` is treated exactly like an unrecognized key: `401`.
- Generate a key value yourself (e.g. `python -c "import secrets; print(secrets.token_urlsafe(32))"`)
  when adding a brand-new client by hand. For an *existing* client, prefer the rotation script
  below over hand-editing — it never wipes an old key out from under an in-flight client.

Restart the orchestrator after editing the file.

## Disabling auth (local testing only)

Set `ORCHESTRATOR_AUTH_ENABLED=false` to turn authentication off entirely — every request is
admitted with no key required, and no `config/api_keys.json` needed. Useful for quickly testing
the orchestrator locally. The orchestrator logs a warning on startup whenever this is set. Do not
use this on anything reachable beyond your own machine.

## Rotating a key

```
llm-home-lab-rotate-key <client_id> [--grace-period-hours 24] [--keys-file PATH]
```

This generates a new key for `client_id`, appends it (never shown again after this run — copy it
now), and stamps that client's other non-expiring keys with `expires_at = now + grace period`
(default 24h) so they keep working until you've redistributed the new key and restarted every
consumer. Keys that were already set to expire are left alone. Exits non-zero with a message on
stderr if `client_id` isn't in the file.

The new key is printed to **stdout only** — it is never written to a log file, including the
audit log described below.

## Audit logging

Every authentication/authorization decision on a non-exempt path is logged via a dedicated
`llm_home_lab.audit` logger, one line per request:

```
client_id=<id-or-"unknown"> method=<METHOD> path=<path> outcome=<allowed|blocked> reason=<ok|missing_token|invalid_token|path_not_allowed>
```

`ToolStateManager.record_terminal_invocation`/`record_filesystem_invocation` calls also log one
`llm_home_lab.audit` line each (`client_id`, `session_id`, `tool_id`, `action=recorded`) — the
caller-supplied `client_id` is a required argument on both methods; there is no "unknown" case
here since these are trusted Python calls, not untrusted network requests.

Configure log level/destination the same way as the rest of this app (standard `logging` module
config) — nothing audit-specific to set up beyond that.

## Secret hygiene

- Never commit `config/api_keys.json` (or wherever `ORCHESTRATOR_API_KEYS_FILE` points) — it's in
  `.gitignore`. If you think a key leaked into git history, rotate it immediately and scrub the
  history.
- Filesystem permissions on the keys file are your responsibility — this codebase does not
  encrypt the file at rest. Restrict it to the user the orchestrator runs as (e.g. `chmod 600`).
- Don't log a raw key anywhere. The rotation script prints a new key to stdout exactly once by
  design; don't pipe that output somewhere it gets persisted in plaintext (shell history,
  unencrypted notes, chat logs).

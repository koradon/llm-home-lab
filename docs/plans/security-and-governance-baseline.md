# Security and Governance Baseline Plan

## Status

draft

## Related

- Spec: [security-and-governance-baseline](../specs/security-and-governance-baseline.md)
- Depends on: [`api/app.py`](../../src/llm_home_lab/api/app.py) (the five endpoints this plan
  brings under auth: `/v1/chat/completions`, `/v1/nodes/register`,
  `/v1/nodes/{host_id}/heartbeat`, `DELETE /v1/nodes/{host_id}`, `GET /v1/nodes`)
- Depends on: [`state/tool_state_manager.py`](../../src/llm_home_lab/state/tool_state_manager.py)
  (`record_terminal_invocation`/`record_filesystem_invocation`, gaining `client_id`)
- Issue: #11 (Implement security and governance baseline, M4)

## Scope

Build an `ApiKeyStore` (`src/llm_home_lab/security/`) for Bearer-token authentication and
path-prefix authorization, wire it into `create_app` as a middleware ahead of every route except
`/health/live`/`/health/ready`, add a `client_id` parameter + audit logging to
`ToolStateManager`'s two `record_*` methods, ship a rotation script, and write
`docs/security-baseline.md` for contributors.

Concrete decisions for this plan (resolving the spec's file-format-level detail):

- **Key file schema** (JSON, path via `ORCHESTRATOR_API_KEYS_FILE`, default
  `./config/api_keys.json`):
  ```json
  {
    "clients": [
      {
        "client_id": "chat-client",
        "allowed_path_prefixes": ["/v1/chat/completions"],
        "keys": [{"key": "sk-...", "expires_at": null}]
      }
    ]
  }
  ```
  `expires_at` is `null` for a key with no planned expiry, or an ISO-8601 timestamp string.
- **Rotation script** (`src/llm_home_lab/security/rotate_keys.py`, exposed as a
  `llm-home-lab-rotate-key` console script alongside the existing `llm-home-lab` entry in
  `pyproject.toml`): `llm-home-lab-rotate-key <client_id> [--grace-period-hours 24] [--keys-file
  PATH]` — generates a new key (`secrets.token_urlsafe(32)`), appends it to that client's `keys`
  list with `expires_at: null`, stamps every *other* currently-non-expiring key for that client
  with `expires_at = now + grace_period` (already-expiring keys are left alone), writes the file
  back, and prints the new key to stdout (the only time it's shown in plaintext — never logged).
  Exits non-zero with a clear message if `client_id` isn't found in the file.
- **Baseline doc** (`docs/security-baseline.md`, top-level under `docs/` — a one-off contributor
  reference, not a recurring content type, so no new subfolder per `DECISION_RULES.md`): covers
  how to add a client, how auth/authorization work, how to rotate a key with the script, where the
  audit log lives, and explicit secret-hygiene guidance (never commit `config/api_keys.json`; add
  it to `.gitignore`; filesystem permissions are the operator's responsibility for encryption at
  rest, per the spec's Open Questions).

Out of scope for this plan (per the spec's Open Questions):

- Method-specific authorization (only path-prefix, method-agnostic).
- Encryption at rest for the key file.
- Hot-reloading the key file without a restart.

## Steps

1. **Security package** (`src/llm_home_lab/security/`, new package sibling to `registry/`,
   `scheduling/`, `health/`) — `__init__.py` exporting the public surface.
2. **Security models** (`src/llm_home_lab/security/models.py`) — `ApiKey` (`key: str`,
   `expires_at: datetime | None`), `ClientConfig` (`client_id`, `allowed_path_prefixes: list[str]`,
   `keys: list[ApiKey]`), `ClientIdentity` (`client_id`, `allowed_path_prefixes`) — the resolved,
   public-facing identity `authenticate` returns (deliberately not exposing raw `ApiKey`s beyond
   this module).
3. **`ApiKeyStore`** (`src/llm_home_lab/security/key_store.py`) — constructed from a list of
   `ClientConfig` (pure, no file IO — fully unit-testable); `authenticate(bearer_token, at)`,
   `is_authorized(identity, path)`. A separate `ApiKeyStore.from_file(path)` classmethod handles
   the JSON parsing at the edge, keeping the core class free of IO concerns.
4. **Audit logging** — `audit_logger = logging.getLogger("llm_home_lab.audit")` defined in
   `api/app.py` (mirroring where `access_logger` already lives) and again in
   `tool_state_manager.py` (mirroring `health_logger`'s per-module pattern) — same logger name,
   two definition sites, matching this codebase's existing convention.
5. **App wiring** (`src/llm_home_lab/api/app.py`) — `create_app` gains a required `key_store:
   ApiKeyStore` parameter. New `@app.middleware("http") enforce_auth` runs before the existing
   `log_requests` middleware for every path except `/health/live`/`/health/ready`: extracts the
   `Authorization: Bearer <token>` header, calls `key_store.authenticate`, then
   `key_store.is_authorized` against `request.url.path`; on failure returns a `401`/`403`
   `_error_response` directly (short-circuiting before the route handler) and logs one
   `audit_logger` line; on success logs the `reason="ok"` line and lets the request proceed.
6. **`ToolStateManager` wiring** (`src/llm_home_lab/state/tool_state_manager.py`) —
   `record_terminal_invocation`/`record_filesystem_invocation` gain a required `client_id: str`
   parameter (first positional arg after `self`, ahead of `session_id`, since identity is the more
   fundamental piece of context) and log one `audit_logger` line each before delegating to the
   store as today.
7. **Entry point** (`src/llm_home_lab/main.py`) — `create_default_app` loads
   `ApiKeyStore.from_file(os.environ.get("ORCHESTRATOR_API_KEYS_FILE", "./config/api_keys.json"))`
   and passes it to `create_app`.
8. **Rotation script** (`src/llm_home_lab/security/rotate_keys.py` + `pyproject.toml` console
   script entry).
9. **Baseline doc** (`docs/security-baseline.md`).
10. **Existing test updates** — every `create_app(...)` call site (`test_gateway.py`,
    `test_health.py`, `test_failover.py`, `test_node_registry_endpoints.py`,
    `test_capacity_scheduling.py`, `test_main.py`) needs a `key_store` fixture with a
    permissive client (`allowed_path_prefixes=["/"]` or similar catch-all) so existing behavior
    tests don't have to carry auth concerns themselves. `test_tool_state_manager.py` needs a
    `client_id` added to its `record_*` calls.
11. **Verification** — full test suite (`uv run pytest --cov=llm_home_lab`), `uv run ruff check
    .`, `uv run ruff format --check .`, `uv run mypy src`.

Implemented test-first (`tdd` skill), one behavior at a time: `ApiKeyStore` fully first (pure,
no dependency on FastAPI), then the `enforce_auth` middleware against `TestClient`, then
`ToolStateManager`'s `client_id` addition, then the rotation script, then the doc last.

## Risks

- **Existing-test churn is broad but shallow**: nearly every test file that builds a `create_app`
  needs a one-line `key_store` addition. Do this as a single mechanical pass per file (add a
  shared permissive `_key_store()` helper per test module) rather than repeating the
  `ClientConfig`/`ApiKeyStore` construction inline everywhere.
- **Prefix matching false-positives**: a naive `path.startswith(prefix)` check means an
  `allowed_path_prefixes` of `["/v1/nodes"]` also (correctly) matches `/v1/nodes-something-else`
  if such a path ever existed. Not a real risk today (no such path exists), but worth a comment at
  the `is_authorized` call site so a future route addition doesn't accidentally violate the
  assumption. Prefer prefixes that already end in a natural boundary (`/v1/nodes`, not `/v1/node`).
- **Audit log volume**: logging one line per request (even successes) is new, unbounded-by-default
  log volume compared to today. Acceptable at home-lab request rates; revisit (e.g. sample
  `reason="ok"` entries) only if log volume becomes a real operational problem.
- **`client_id` becoming a required `ToolStateManager` param is a breaking change**: any existing
  caller of `record_terminal_invocation`/`record_filesystem_invocation` (currently only tests)
  must be updated in the same change, not left broken.

## Open Questions

- Same as the spec's: method-specific authorization, encryption at rest, and hot-reload are
  deferred until a concrete need appears.

## Addendum: auth on/off toggle

Added after initial implementation, per user request for faster local testing: `create_app` gained
`auth_enabled: bool = True` and `key_store` became `ApiKeyStore | None = None`; `create_app` raises
`ValueError` if `auth_enabled=True` and `key_store=None`. `main.py` reads
`ORCHESTRATOR_AUTH_ENABLED` (default `true`) and logs a warning on startup whenever it's disabled.
See the spec's updated Requirements/Behavior sections for the full contract.

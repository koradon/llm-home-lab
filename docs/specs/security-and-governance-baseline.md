# Security and Governance Baseline

## Status

draft

## Summary

Static Bearer API-key authentication and path-prefix authorization for the orchestrator's HTTP
surface, plus a dedicated `llm_home_lab.audit` logger that records every authorization decision
and every `ToolStateManager` invocation with the resolved client identity attached. Rejected
JWT/OAuth as unjustified complexity for a single-process home-lab orchestrator with no external
identity provider — the actual pain point (key management) is solved by a rotation script (see
Related), not by a different auth scheme.

## User stories

- As an operator, I want every request to require a valid API key, so that only clients I've
  explicitly provisioned can reach the orchestrator.
- As an operator, I want a client's key scoped to the endpoints it actually needs (e.g. a chat
  client can't register nodes), so that a leaked key's blast radius is limited to what that
  client was meant to do.
- As an operator, I want every blocked request logged with whatever identity it presented (or
  "unknown" if none), so that I can see attempted unauthorized access without reproducing it.
- As an operator, I want a `ToolStateManager` invocation's audit entry to show which client
  reported it, so that tool-state history is traceable to a caller, not anonymous.
- As an operator, I want to rotate a client's key without an access gap, so that redistributing a
  new key doesn't require perfectly synchronized timing with a restart.
- As a contributor, I want a single baseline document describing how auth/authorization/secrets
  work here, so that I don't have to reverse-engineer the convention from code.

## Requirements

- Provide an `ApiKeyStore` loaded once at startup from a JSON file (path via
  `ORCHESTRATOR_API_KEYS_FILE` env var, matching this codebase's existing
  read-env-var-at-startup convention — see `main.py`). No hot-reload: a rotated key file requires
  a restart to take effect, exactly like every other config value in this codebase
  (`LMSTUDIO_BASE_URL`, etc.).
- File schema: a list of clients, each with a `client_id`, one or more `keys` (each an opaque
  string plus an optional `expires_at` timestamp), and `allowed_path_prefixes` (a list of path
  prefixes this client's requests may reach).
- `ApiKeyStore.authenticate(bearer_token, at) -> ClientIdentity | None` — resolves a presented
  token to the `ClientIdentity` (`client_id`, `allowed_path_prefixes`) owning it, or `None` if no
  non-expired key matches. `at` is caller-supplied (no internal wall-clock read), matching
  `HealthMonitor`'s determinism convention, so expiry behavior is deterministic in tests.
- `ApiKeyStore.is_authorized(identity, path) -> bool` — `True` if `path` starts with any of the
  identity's `allowed_path_prefixes`.
- `create_app` gains an `auth_enabled: bool = True` parameter and a `key_store: ApiKeyStore | None
  = None` parameter — auth is on by default (not silently permissive), but can be explicitly
  turned off (e.g. `ORCHESTRATOR_AUTH_ENABLED=false`) for fast local testing or deployments that
  don't need it. `create_app` raises `ValueError` if `auth_enabled=True` and `key_store=None` —
  there is no silent "forgot to configure" fallback; disabling auth is only ever the result of an
  explicit, visible choice (`main.py` also logs a warning when it does so).
- Every request except `/health/live` and `/health/ready` (exempt — standard liveness/readiness
  probe convention, and these reveal no client-specific data) is authenticated: missing or
  unrecognized `Authorization: Bearer <token>` → `401`; a recognized token whose identity's
  `allowed_path_prefixes` doesn't cover the request path → `403`.
- Every authorization decision on a non-exempt path — allowed or blocked — is logged via a
  dedicated `llm_home_lab.audit` logger, `%s`-formatted single-line `key=value` entries mirroring
  `access_logger` (`api/app.py`) and `health_logger` (`health/monitor.py`): `client_id=%s
  method=%s path=%s outcome=%s reason=%s` (`client_id="unknown"` when no valid identity was
  resolved; `reason` e.g. `"missing_token"`, `"invalid_token"`, `"path_not_allowed"`, `"ok"`).
- `ToolStateManager.record_terminal_invocation` and `record_filesystem_invocation` gain a
  required `client_id: str` parameter (breaking change to their signatures — both are still
  unwired from HTTP per the M2 decision, so this only affects direct Python callers) and each
  call logs one `llm_home_lab.audit` entry: `client_id=%s session_id=%s tool_id=%s action=%s`.

## Behavior

**No token is a 401, logged as such**: a request to a non-exempt path with no `Authorization`
header (or a malformed one) is rejected with `401` before reaching the route handler, and audited
with `client_id="unknown"` and `reason="missing_token"`.

**An unrecognized or expired token is also a 401**: a `Bearer` token that matches no configured
key, or matches a key whose `expires_at` has passed as of `at`, is treated the same as no token —
`401`, audited with `reason="invalid_token"`.

**A recognized token outside its allowed prefixes is a 403, not a 401**: the identity is known
(and logged by `client_id`, not `"unknown"`) but the request is still blocked, distinguishing "who
are you" failures from "you can't do that" failures in the audit trail.

**A recognized token within its allowed prefixes proceeds normally**: the route handler runs
exactly as it did before this issue; the only observable difference is one additional audit log
line with `reason="ok"`.

**Health probes are never authenticated**: `/health/live` and `/health/ready` remain reachable
without a token and are not audited by this mechanism (they still emit their existing
`access_logger` line from the request-logging middleware).

**A `ToolStateManager` call without a resolvable identity is a caller bug, not a runtime
fallback**: `client_id` is a required parameter with no default — there is no "unknown" case for
tool-state audit entries the way there is for HTTP requests, because the caller is trusted Python
code, not an untrusted network client.

**Auth disabled means every request is admitted unchecked, with no audit trail for it**: when
`auth_enabled=False`, the middleware short-circuits before touching `key_store` or the audit
logger — this is a deliberate, all-or-nothing escape hatch for local testing, not a per-path
exemption mechanism.

**Two valid keys for one client during a rotation window both authenticate successfully**: while
an old key's `expires_at` is still in the future (the rotation grace period), both it and the
newly issued key resolve to the same `ClientIdentity` — rotation does not require synchronized
cutover.

## Acceptance scenarios (BDD)

Keep scenarios in a sibling Gherkin file: `docs/specs/security-and-governance-baseline.feature`.

## Related

- Module: [`api/app.py`](../../src/llm_home_lab/api/app.py) — `access_logger` pattern this spec's
  `audit_logger` mirrors; the five existing endpoints (`/v1/chat/completions`, `/v1/nodes/*`) this
  spec brings under auth.
- Module: [`health/monitor.py`](../../src/llm_home_lab/health/monitor.py) — `health_logger`
  pattern and the caller-supplied-`at` determinism convention `ApiKeyStore.authenticate` follows.
- Module: [`state/tool_state_manager.py`](../../src/llm_home_lab/state/tool_state_manager.py) —
  `record_terminal_invocation`/`record_filesystem_invocation`, gaining the required `client_id`
  parameter.
- Spec: [tool-state-abstraction](tool-state-abstraction.md) — confirms `ToolStateManager` stays
  unwired from HTTP; this spec does not change that.
- Spec: [multi-node-registry-and-scheduler](multi-node-registry-and-scheduler.md) — the
  `/v1/nodes/*` endpoints this spec authorizes.
- Plan: (to be written) `docs/plans/security-and-governance-baseline.md` — will scope the
  rotation script's exact CLI shape and the `docs/security-baseline.md` contributor document.
- Issue: `.plan/milestones/m4-production-hardening/issues/issue-003-security-and-governance-baseline.md`
  (#11)
- Acceptance: `docs/specs/security-and-governance-baseline.feature`

## Open Questions

- Whether `allowed_path_prefixes` ever needs to express more than prefix matching (e.g. method-
  specific rules, so a client could `GET /v1/nodes` but not `DELETE /v1/nodes/{id}`) is deferred
  until a concrete need for that finer grain appears — today authorization is path-prefix-only,
  method-agnostic.
- Whether the key file needs encryption at rest (vs. relying on filesystem permissions) is
  deferred — flagged in the baseline doc as an operator responsibility for now, not solved in
  code.
- Hot-reloading the key file (avoiding a restart on rotation) is deferred; the grace-period design
  already absorbs the "have I redistributed and restarted yet" gap, so a restart-free reload is a
  nice-to-have, not required by this issue's acceptance criteria.

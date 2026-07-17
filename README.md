# llm-home-lab

A local LLM orchestrator: one OpenAI-compatible API endpoint (`/v1/chat/completions`) that
routes requests across one or more local model backends (LM Studio, with more backends possible
later). Point your agent/tool at the orchestrator instead of directly at a model server, and it
handles routing, failover, capacity limits, and auth for you.

```text
Agent (OpenCode / other)  --Bearer key-->  Orchestrator  -->  LM Studio (one or more hosts)
```

## Quickstart

### 1. Set up LM Studio (the model backend)

1. Open LM Studio, load a model.
2. Go to the **Local Server** tab (the `<->` icon in the left sidebar) and click **Start Server**.
3. Note the port shown — it defaults to `1234`. LM Studio now serves an OpenAI-compatible API at
   `http://localhost:1234/v1`.

### 2. Create an API key for the orchestrator

The orchestrator requires a Bearer token on every request except the health endpoints. Create
`config/api_keys.json`:

```json
{
  "clients": [
    {
      "client_id": "my-agent",
      "allowed_path_prefixes": ["/v1/chat/completions"],
      "keys": [{"key": "sk-dev-changeme", "expires_at": null}]
    }
  ]
}
```

Pick your own key value (e.g. `python -c "import secrets; print(secrets.token_urlsafe(32))"`).
See [docs/security-baseline.md](docs/security-baseline.md) for the full format, how to add more
clients, and how to rotate a key later.

**Just testing locally and don't want to deal with keys yet?** Set `ORCHESTRATOR_AUTH_ENABLED=false`
and skip this step entirely — every request is admitted with no key required. Don't do this
outside your own machine.

### 3. Start the orchestrator

Both of these are equally supported — pick whichever fits your setup.

**Option A — uv (runs natively on your machine):**

```bash
uv sync
LMSTUDIO_BASE_URL=http://localhost:1234 uv run llm-home-lab
```

**Option B — Docker:**

```bash
docker compose up --build
```

`docker-compose.yml` points `LMSTUDIO_BASE_URL` at `host.docker.internal` by default, since LM
Studio runs on your host machine, not inside the container. Adjust it if LM Studio runs
elsewhere on your network.

Either way, the orchestrator listens on `:8080` (override with `ORCHESTRATOR_PORT`).

### 4. Send a request

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer sk-dev-changeme" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "any-model-name",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

If you get `401`, check your `Authorization` header and that `config/api_keys.json` exists and
matches `ORCHESTRATOR_API_KEYS_FILE` (see below). If you get `503`, LM Studio's server isn't
reachable at `LMSTUDIO_BASE_URL` — check step 1.

## Adding more model hosts

Once the orchestrator is running, register additional LM Studio instances (other machines on
your network) without restarting it:

```bash
curl -X POST http://localhost:8080/v1/nodes/register \
  -H "Authorization: Bearer <a key allowed on /v1/nodes>" \
  -H "Content-Type: application/json" \
  -d '{
    "host_id": "gpu-box",
    "backend_type": "lmstudio",
    "base_url": "http://gpu-box.home:1234",
    "context_window": 8192,
    "max_concurrent_requests": 4
  }'
```

The orchestrator then routes and load-balances across every registered, healthy host. See
`GET /v1/nodes` to list registered hosts, `POST /v1/nodes/{host_id}/heartbeat` to keep one alive,
and `DELETE /v1/nodes/{host_id}` to remove one.

## Configuration reference

| Var | Default | Purpose |
| --- | --- | --- |
| `ORCHESTRATOR_HOST` | `0.0.0.0` | Gateway bind address |
| `ORCHESTRATOR_PORT` | `8080` | Gateway port |
| `ORCHESTRATOR_API_KEYS_FILE` | `./config/api_keys.json` | Client/key/authorization config |
| `ORCHESTRATOR_AUTH_ENABLED` | `true` | Set `false` to disable auth entirely (local testing only) |
| `LMSTUDIO_BASE_URL` | `http://localhost:1234` | Default LM Studio host registered at startup |
| `LMSTUDIO_TIMEOUT` | `30` | Request timeout (seconds) |
| `LMSTUDIO_MAX_RETRIES` | `2` | Backend retry count |
| `LMSTUDIO_CONTEXT_WINDOW` | `8192` | Context window advertised for routing |
| `LMSTUDIO_MAX_CONCURRENT_REQUESTS` | `4` | Capacity used by the scheduling queue |

## API surface

- `POST /v1/chat/completions` — OpenAI-compatible chat completions (streaming and non-streaming).
- `POST /v1/nodes/register`, `POST /v1/nodes/{host_id}/heartbeat`, `DELETE /v1/nodes/{host_id}`,
  `GET /v1/nodes` — manage model hosts.
- `GET /health/live`, `GET /health/ready` — liveness/readiness (no auth required).

## How it works

- **Routing**: a policy-scored engine picks the best available host per request (e.g. lowest
  latency), with sticky sessions so a conversation stays on the same host.
- **Health**: hosts that fail health checks are excluded from routing until they recover.
- **Capacity**: requests queue (priority + fair scheduling across clients) when every eligible
  host is already at its concurrency limit, instead of failing outright.
- **Auth**: every client has its own key(s) and is restricted to the endpoints it needs; every
  decision is audit-logged.

Design docs and specs for each piece live in `docs/specs/` and `docs/plans/`, and project
planning/issue tracking lives in `.plan/` (synced to GitHub via Planhub) if you want the deeper
detail — none of that is needed to just run the thing.

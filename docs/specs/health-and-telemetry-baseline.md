# Health Endpoints and Telemetry Baseline

## Status

accepted

## Summary

Liveness and readiness endpoints for the orchestrator process, plus structured request logging
(request id, backend id, latency, status) so that routing/failover decisions and operational
monitoring have a reliable signal to act on.

## User stories

- As an operator running local monitoring, I want a liveness endpoint, so that I know the
  orchestrator process itself is up.
- As an operator, I want a readiness endpoint that reflects backend availability, so that I know
  whether the orchestrator can actually serve requests right now.
- As an operator debugging an issue, I want every request logged with a correlation id, backend
  id, latency, and status, so that I can trace one request's path through logs.

## Requirements

- Expose `GET /health/live`: returns 200 as long as the process is running; it does not depend
  on backend state.
- Expose `GET /health/ready`: returns 200 when the configured backend is healthy, 503 when it is
  not; the response body reports per-backend health detail.
- Extend the `ChatBackend` port with a health check so the gateway can query backend
  availability without depending on a concrete backend implementation.
- Add request logging middleware that, for every request, emits one structured log line
  containing: request id (read from an inbound `X-Request-ID` header if present, otherwise
  generated), HTTP method and path, status code, and latency in milliseconds. The request id is
  also returned in the response's `X-Request-ID` header.
- Chat completion requests additionally log the backend id/host that served (or failed to
  serve) the request.

## Behavior

**Liveness**: `GET /health/live` always returns `{"status": "ok"}` with HTTP 200 while the
process is running — it never calls out to a backend.

**Readiness, healthy backend**: `GET /health/ready` calls the configured backend's health check;
if healthy, returns HTTP 200 with `{"status": "ok", "backends": [{"id": ..., "healthy": true}]}`.

**Readiness, unhealthy backend**: if the backend's health check fails or the backend reports
unhealthy, returns HTTP 503 with `{"status": "unavailable", "backends": [{"id": ..., "healthy":
false, "detail": ...}]}`.

**Request logging**: every request (health checks included) produces exactly one structured log
line after the response is generated, carrying request id, method, path, status code, and
latency. A request arriving with an `X-Request-ID` header reuses that id instead of generating a
new one, so callers can correlate across services.

**Chat completion logging**: in addition to the generic request log line, a successful or failed
backend dispatch logs the backend id/host and outcome, linked by the same request id.

**Edge cases**:

- Concurrent requests must not leak or mix up request ids (each request gets its own id, request
  id is not shared mutable state).
- A slow but eventually-successful backend call is still logged with its actual latency, not
  cut short.

## Acceptance scenarios (BDD)

Keep scenarios in a sibling Gherkin file: `docs/specs/health-and-telemetry-baseline.feature`.

## Related

- Plan: [orchestrator-program](../plans/orchestrator-program.md) (M1 — health and telemetry
  baseline)
- Spec: [openai-compatible-api-gateway](openai-compatible-api-gateway.md) (gateway this health
  check attaches to)
- Spec: [lmstudio-backend-adapter](lmstudio-backend-adapter.md) (backend whose health this
  reports)
- Acceptance: `docs/specs/health-and-telemetry-baseline.feature`

## Open Questions

- Should `/health/ready` cache the backend health check result for a short window to avoid
  hammering the backend on frequent liveness probes, or is a fresh check per call acceptable at
  this scale?
- Exact structured log format (plain key=value vs. JSON lines) — deferred to whatever the chosen
  logging setup makes easiest; not a wire contract, so lower stakes than the HTTP behavior above.

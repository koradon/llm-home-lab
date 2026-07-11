Feature: Health endpoints and telemetry baseline
  Operators can check orchestrator liveness/readiness and trace requests via structured logs.

  # Related spec: docs/specs/health-and-telemetry-baseline.md

  Scenario: Liveness always succeeds
    When a client calls GET /health/live
    Then the response has HTTP status 200
    And the response body reports status "ok"

  Scenario: Readiness reports a healthy backend
    Given the configured backend reports itself healthy
    When a client calls GET /health/ready
    Then the response has HTTP status 200
    And the response body lists the backend as healthy

  Scenario: Readiness reports an unhealthy backend
    Given the configured backend reports itself unhealthy
    When a client calls GET /health/ready
    Then the response has HTTP status 503
    And the response body lists the backend as unhealthy with a detail message

  Scenario: Every request produces one structured log line
    When a client calls any endpoint
    Then exactly one structured log line is emitted for that request
    And the log line includes a request id, method, path, status code, and latency

  Scenario: Inbound request id is reused
    When a client calls an endpoint with an "X-Request-ID" header
    Then the response's "X-Request-ID" header matches the inbound value
    And the structured log line uses that same request id

  Scenario: Request id is generated when absent
    When a client calls an endpoint without an "X-Request-ID" header
    Then the response includes a generated "X-Request-ID" header

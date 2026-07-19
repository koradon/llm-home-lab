Feature: Security and governance baseline
  Every non-exempt request must present a valid, scoped API key; every authorization decision and
  every tool-state invocation is recorded in the audit log with its resolved client identity.

  # Related spec: docs/specs/security-and-governance-baseline.md

  Scenario: A request with no Authorization header is rejected and audited
    Given a request to a protected path with no Authorization header
    When the request is handled
    Then the response is 401
    And an audit entry is recorded with client_id "unknown" and reason "missing_token"

  Scenario: A request with an unrecognized token is rejected and audited
    Given a request to a protected path with a Bearer token that matches no configured key
    When the request is handled
    Then the response is 401
    And an audit entry is recorded with client_id "unknown" and reason "invalid_token"

  Scenario: A request with an expired token is rejected and audited
    Given a client's only key has an expires_at in the past relative to the current time
    When a request presents that expired key
    Then the response is 401
    And an audit entry is recorded with reason "invalid_token"

  Scenario: A recognized token outside its allowed prefixes is forbidden, not unauthorized
    Given a client whose allowed path prefixes do not cover the requested path
    When that client's valid token is presented for that request
    Then the response is 403
    And an audit entry is recorded with that client's client_id and reason "path_not_allowed"

  Scenario: A recognized token within its allowed prefixes is admitted
    Given a client whose allowed path prefixes cover the requested path
    When that client's valid token is presented for that request
    Then the request reaches the route handler
    And an audit entry is recorded with that client's client_id and reason "ok"

  Scenario: Health probes bypass authentication
    Given no Authorization header is presented
    When a request is made to /health/live or /health/ready
    Then the response is not 401 or 403

  Scenario: Two valid keys for the same client both authenticate during a rotation window
    Given a client has an old key with a future expires_at and a newly issued key
    When either key is presented
    Then both requests resolve to the same client identity

  Scenario: A tool-state invocation records the caller's identity in the audit trail
    Given a terminal or filesystem invocation is recorded with a client_id
    When that invocation is recorded
    Then an audit entry is written including that client_id, the session_id, and the tool_id

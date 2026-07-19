Feature: Monitoring, SLOs, and alerting
  The orchestrator aggregates request outcomes, latency, failover success, host saturation, and
  token usage into SLIs, exposes them for scraping, and evaluates alert rules against them.

  # Related spec: docs/specs/20260719-monitoring-slos-and-alerting.md

  Scenario: /metrics exposes current SLIs in Prometheus format
    Given the orchestrator has served some requests and has registered hosts
    When a client sends GET /metrics
    Then the response is 200 with content-type "text/plain; version=0.0.4"
    And the body includes availability, p95 latency, host saturation, queue depth, and token usage

  Scenario: /metrics works before any traffic has been recorded
    Given a freshly started orchestrator with no recorded requests
    When a client sends GET /metrics
    Then the response is 200
    And the exposed gauges and counters are zero-valued, not an error

  Scenario: /metrics is reachable without authentication
    Given auth is enabled and no Authorization header is presented
    When a client sends GET /metrics
    Then the response is not 401 or 403

  Scenario: Availability only counts requests within the rolling window
    Given a burst of failed requests occurred longer ago than the rolling window
    When a snapshot is taken at the current time
    Then those failed requests no longer affect the availability SLI

  Scenario: Failover success rate ignores requests where no failover was in play
    Given a request was served by the first candidate with no host excluded for health
    When the failover success rate SLI is computed
    Then that request is not counted in either the numerator or the denominator

  Scenario: Failover success rate counts a request where an unhealthy host was excluded
    Given a request was served successfully after at least one candidate host was excluded for
      health
    When the failover success rate SLI is computed
    Then that request counts as a success in the SLI

  Scenario: An alert fires exactly once on breach, not on every evaluation
    Given a metric stays above its rule's threshold across several consecutive evaluations
    When the alert evaluator runs for each of those evaluations
    Then exactly one firing AlertEvent is logged, on the first breach

  Scenario: An alert logs a resolved transition when the metric recovers
    Given an alert is currently firing
    When a later evaluation shows the metric back within threshold
    Then a resolved AlertEvent is logged for that rule

  Scenario: A critical alert carries a runbook link
    Given a critical-severity alert rule fires
    When the AlertEvent is logged
    Then it includes a runbook_url pointing into docs/runbooks/

  Scenario: An SLO burn rule and a threshold rule on the same metric transition independently
    Given both a slo_burn rule and a threshold rule are configured on availability
    When one rule's condition breaches and the other's does not
    Then only the breaching rule's AlertEvent fires

  Scenario: A missing alert rules file yields no alerts, not a crash
    Given ORCHESTRATOR_ALERT_RULES_FILE points to a file that does not exist
    When the orchestrator starts
    Then it starts successfully with an empty rule set
    And /metrics still reports SLIs normally

  Scenario: A malformed alert rules file fails startup loudly
    Given the alert rules file contains an unknown rule kind
    When the orchestrator starts
    Then startup raises an error instead of loading a partial rule set

  Scenario: GET /v1/alerts requires authentication like other operational endpoints
    Given auth is enabled and no Authorization header is presented
    When a client sends GET /v1/alerts
    Then the response is 401

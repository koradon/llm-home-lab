Feature: TUI operator dashboard
  A terminal client polls the orchestrator's existing diagnostic endpoints and renders live node
  health, firing alerts, and queue/token usage.

  # Related spec: docs/specs/20260719-tui-operator-dashboard.md

  Scenario: Dashboard renders registered nodes
    Given the orchestrator has one or more registered hosts
    When the TUI polls GET /v1/nodes
    Then the Nodes panel shows each host's id, backend type, in-flight/capacity, and last seen time

  Scenario: Dashboard renders currently firing alerts
    Given an alert rule is currently firing on the orchestrator
    When the TUI polls GET /v1/alerts
    Then the Alerts panel shows the rule name, severity, value, threshold, and runbook link

  Scenario: Dashboard renders queue depth and token usage
    Given the orchestrator has recorded queue depth and token usage
    When the TUI polls GET /metrics
    Then the Queue & Tokens panel shows the parsed queue depth and per-host token totals

  Scenario: A connection failure shows a banner and keeps polling
    Given the orchestrator is unreachable
    When a scheduled poll fails
    Then the TUI shows a connection-error banner
    And the next scheduled poll is attempted rather than the TUI exiting

  Scenario: An unauthorized key shows a distinct auth error
    Given the configured API key is missing or invalid
    When a poll receives a 401 or 403 response
    Then the TUI shows a not-authorized banner distinct from a connection-error banner

  Scenario: An unexpected metrics shape degrades one panel, not the whole dashboard
    Given the /metrics response is missing an expected metric line
    When the TUI parses the scrape
    Then the affected Queue & Tokens row shows "unavailable"
    And the Nodes and Alerts panels continue to render normally

  Scenario: The TUI never mutates orchestrator state
    Given the TUI is running against a live orchestrator
    When it polls on its interval
    Then it only ever issues GET requests to /v1/nodes, /v1/alerts, and /metrics

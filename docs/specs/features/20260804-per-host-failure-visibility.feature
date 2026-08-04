Feature: Per-host failure visibility
  GET /v1/nodes and the TUI Nodes table surface a per-host recent-failure count, sourced from
  HealthMonitor's existing bounded probe history, so an operator can identify a degrading host
  directly instead of reading logs.

  # Related spec: docs/specs/20260804-per-host-failure-visibility.md

  Scenario: A host with recent failures reports a non-zero count while still healthy
    Given a registered host has 2 failed probes out of its last 20 recorded probes
    And it has not crossed the failure_threshold
    When GET /v1/nodes is queried
    Then that host's status is "online"
    And that host's health.recent_failures is 2

  Scenario: A host with no probe history reports zero failures
    Given a registered host has never been probed
    When GET /v1/nodes is queried
    Then that host's status is "unknown"
    And that host's health.recent_failures is 0

  Scenario: An old failure ages out of the recent-failures window
    Given a host's failure history is older than the bounded probe window HealthMonitor retains
    When GET /v1/nodes is queried
    Then that host's health.recent_failures no longer counts the aged-out failure

  Scenario: The TUI Nodes table displays the failure count per host
    Given GET /v1/nodes reports health.recent_failures greater than 0 for a host
    When the TUI renders the Nodes table
    Then that host's row shows a non-zero value in the errors column

  Scenario: Recent failures do not affect routing candidacy
    Given a host has a non-zero recent-failures count but is still is_healthy
    When routing selects a candidate for a new request
    Then that host remains eligible for selection

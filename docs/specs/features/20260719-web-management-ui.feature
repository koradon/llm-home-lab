Feature: Web management UI
  A separate browser-facing service polls the orchestrator's diagnostic endpoints, persists a
  short rolling history in its own SQLite store, and serves a dashboard with current state and
  trends.

  # Related spec: docs/specs/20260719-web-management-ui.md

  Scenario: Dashboard shows current node state
    Given the orchestrator has registered hosts
    When a browser requests GET /api/nodes
    Then the response reflects the most recently polled node snapshot

  Scenario: Dashboard shows historical trend data
    Given samples have been collected over the retention window
    When a browser requests GET /api/series for a metric
    Then the response includes points spanning the requested time range

  Scenario: A stale snapshot is served with a visible marker when the orchestrator is unreachable
    Given the orchestrator becomes unreachable after at least one successful poll
    When a browser requests GET /api/nodes
    Then the last successfully polled snapshot is returned
    And the response includes a stale_since timestamp

  Scenario: Historical data survives a web UI restart
    Given samples were persisted before a restart
    When the web UI restarts and a browser requests GET /api/series
    Then previously persisted samples within the retention window are still returned

  Scenario: Samples older than the retention window are pruned
    Given a sample is older than WEBUI_RETENTION_HOURS
    When the next poll cycle runs
    Then that sample is no longer returned by GET /api/series

  Scenario: Web UI access requires its own key, not the orchestrator's
    Given a valid orchestrator API key but no valid web UI key
    When a browser requests GET /api/nodes without a valid web UI session/key
    Then the response is 401

  Scenario: The web UI never proxies chat completions
    Given the web UI is running
    When its API surface is inspected
    Then it exposes no endpoint that forwards to /v1/chat/completions or any LM Studio backend

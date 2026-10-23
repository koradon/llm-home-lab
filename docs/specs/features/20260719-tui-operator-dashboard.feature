Feature: TUI operator dashboard
  A terminal client polls the orchestrator's existing diagnostic endpoints and renders live node
  health, completion metrics, and node load.

  # Related spec: docs/specs/20260719-tui-operator-dashboard.md

  Scenario: Dashboard renders registered nodes
    Given the orchestrator has one or more registered hosts
    When the TUI polls GET /v1/nodes
    Then the Nodes panel shows each host's id, backend type, status with its in-flight/capacity counter, and last seen time

  Scenario: Dashboard keeps only Nodes and Node Load
    Given nodes are fully utilized and alert rules are firing
    When the dashboard refreshes
    Then the Nodes and Node Load panels are displayed
    And no Alerts or Queue & Tokens panel or replacement summary is displayed
    And the Nodes table has no external-load column

  Scenario: The status cell shows the load counter
    Given "host-a" is online with 8 in-flight requests and capacity 8
    And "host-b" is offline with 0 in-flight requests and capacity 5
    When the dashboard refreshes
    Then the status cells read "online 8/8" and "offline 0/5"
    And the Nodes table has no separate in-flight/capacity column

  Scenario: The counter is coloured by load and availability
    Given "host-a" is online and busy, "host-b" is online and idle
    And "host-c" is offline and "host-d" is unknown
    When the dashboard refreshes
    Then the busy counter is bold yellow and the idle counter is unstyled
    And the offline and unknown counters are dim
    And each status word keeps its own colour

  Scenario: The load chart label shows the counter and percent
    Given "host-a" has 1 in-flight request and capacity 3, and "host-c" has capacity 0
    When the dashboard refreshes
    Then the labels read "host-a · 1/3 · 33%" and "host-c · 0/0 · n/a"

  Scenario: Node Load has one series per node
    Given a node has 8 in-flight requests and capacity 8
    When the dashboard refreshes
    Then that node has one full-height green load chart with a sample of 1.0
    And no own/external split is displayed

  Scenario: A connection failure shows a banner and keeps polling
    Given the orchestrator is unreachable
    When a scheduled poll fails
    Then the TUI shows a connection-error banner
    And the next scheduled poll is attempted rather than the TUI exiting

  Scenario: An unauthorized key shows a distinct auth error
    Given the configured API key is missing or invalid
    When a poll receives a 401 or 403 response
    Then the TUI shows a not-authorized banner distinct from a connection-error banner

  Scenario: The TUI's polling loop never mutates orchestrator state
    Given the TUI is running against a live orchestrator
    When it polls on its interval
    Then it only ever issues GET requests to /v1/nodes and /metrics
    And it only registers, edits, or deregisters a node in response to explicit operator action
      in the Nodes panel, never as part of the polling loop itself

  Scenario: The operator reorders nodes and the order is persisted on the node record
    Given the Nodes table lists host-b, host-c, host-a in the server's order
    When the operator presses "d" on host-b
    Then the TUI PATCHes each node's "position" through the existing node API
    And the Nodes table and the Node Load charts both list host-c, host-b, host-a

  Scenario: A column sort is a session-only view
    Given the Nodes table is in the manual order
    When the operator presses "s" and "r"
    Then the Nodes table and the Node Load charts are sorted by the chosen column and direction
    And no node's "position" is changed
    And pressing "o" returns both to the manual order

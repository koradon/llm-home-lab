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

  Scenario: Multi-model nodes show summary and per-model child rows starting at status
    Given a router node "mini-1" has two loaded models "gemma-e4b" (3/8 busy) and "eurollm-22b" (1/2 busy)
    And a plain single-model node "cachy-1" has no model limits
    When the dashboard refreshes
    Then the Nodes table has no trailing "models" column
    And "mini-1" renders 3 rows: a summary row ("online 4/10") followed by two indented model rows starting at the status column with empty host_id and per-model statistics
    And the additive metrics on the summary row equal the sum of its model rows
    And "cachy-1" renders a single row
    And the Node Load panel shows compact 2-row per-model sub-charts beneath "mini-1"

  Scenario: A failed model stays isolated from node reachability and capacity
    Given a router node "mini-2" is online with "gemma-e4b" loaded (3/8, 0 errors), "broken-70b" failed (0/2, 3 errors), and 2 node transport errors
    When the dashboard refreshes
    Then "mini-2" stays "online 3/8" with errors "5 (2 node)"
    And only the "broken-70b (failed)" child row is coloured red

  Scenario: Selecting a child model row targets its parent node
    Given "mini-1" renders a summary row and two child model rows
    When the operator moves the cursor onto a child model row and presses "e"
    Then the edit modal opens for "mini-1"

  Scenario: The operator views and edits per-model limits in the node form
    Given a node has allowed_models "gemma, eurollm" and a per-model limit of 4 slots on "eurollm"
    When the operator presses "e" to edit the node
    Then the form shows one models table row for "gemma" with blank inputs and one row for "eurollm" with "4"
    When the operator sets "gemma" max slots to "8" and saves
    Then the TUI PATCHes the node with model_limits containing both overrides
    And invalid or non-positive values keep the modal open with a clear error message

  Scenario: Editing retains a disallowed model's overrides without widening the allowlist
    Given a node allows "gemma" and has a limit and alias for "standby"
    When the operator opens and saves the node form without changing the models
    Then "standby" is shown as not allowed
    And the saved allowlist contains only "gemma"
    And the limit and alias for "standby" are retained

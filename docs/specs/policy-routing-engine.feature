Feature: Policy-based routing engine
  The orchestrator selects which configured backend serves each chat completion request by
  scoring candidates against declared policy rules, with an optional sticky mode that keeps a
  session on the backend it started with.

  # Related spec: docs/specs/policy-routing-engine.md

  Scenario: Latency-preferring rule selects the fastest healthy candidate
    Given two healthy backends with different latency estimates
    And a routing policy that scores lower latency higher
    When the engine selects a backend for a request
    Then the backend with the lower latency estimate is chosen

  Scenario: Task-type rule only contributes to matching requests
    Given a routing policy with a rule scoped to task_type "code"
    And a request with task_type "code"
    When the engine selects a backend for that request
    Then the rule's score contributes to the decision

  Scenario: A request without the matching task type does not receive that rule's score
    Given a routing policy with a rule scoped to task_type "code"
    And a request with no task_type declared
    When the engine selects a backend for that request
    Then the rule does not contribute to any candidate's score

  Scenario: Token budget excludes undersized backends
    Given a candidate backend whose context window is smaller than the request's token budget
    When the engine selects a backend for that request
    Then that candidate is excluded from scoring

  Scenario: No candidates left raises an error
    Given every candidate backend is excluded by token budget
    When the engine selects a backend for that request
    Then a NoAvailableBackendError is raised

  Scenario: Routing decisions are reproducible
    Given a fixed request, candidate set, and latency estimates
    When the engine selects a backend for that request twice
    Then both routing decisions are identical

  Scenario: Sticky routing records the winning backend on the first turn
    Given sticky routing is enabled
    And a new session with no prior sticky backend
    When the engine selects a backend for that session's first request
    Then the winning backend is recorded as that session's sticky backend

  Scenario: Sticky routing reuses the recorded backend on later turns
    Given sticky routing is enabled
    And a session with a recorded sticky backend that is still healthy
    When the engine selects a backend for that session's next request
    Then the recorded sticky backend is chosen without re-scoring

  Scenario: Sticky routing falls back when the recorded backend is unhealthy
    Given sticky routing is enabled
    And a session's recorded sticky backend is no longer among the healthy candidates
    When the engine selects a backend for that session's next request
    Then the engine scores among the remaining healthy candidates

  Scenario: Sticky routing can be disabled
    Given sticky routing is disabled
    And a session with a recorded sticky backend
    When the engine selects a backend for that session's next request
    Then the request is scored normally instead of reusing the recorded backend

  Scenario: Routing without a session id never applies sticky logic
    Given sticky routing is enabled
    And a request with no session id
    When the engine selects a backend for that request
    Then the decision is made by policy scoring alone

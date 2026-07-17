Feature: Tool state abstraction
  The orchestrator stores a caller-reported history of terminal and filesystem tool invocations
  per session, ordered chronologically across both tool types, so switching models never loses
  tool continuity.

  # Related spec: docs/specs/tool-state-abstraction.md

  Scenario: Recording and reading terminal history round-trips
    Given a session exists
    When the caller records a terminal invocation running "npm test" in "/repo"
    Then reading the terminal history returns that invocation

  Scenario: Recording and reading filesystem history round-trips
    Given a session exists
    When the caller records a filesystem "write" invocation on path "app.py"
    Then reading the filesystem history returns that invocation

  Scenario: Terminal and filesystem invocations share one chronological sequence
    Given a session exists
    When the caller records a terminal invocation, then a filesystem invocation, then another terminal invocation
    Then the terminal history contains both terminal invocations in their original relative order
    And the filesystem history contains the one filesystem invocation

  Scenario: Terminal state reflects the most recent terminal invocation
    Given a session has two recorded terminal invocations with different working directories
    When the caller reads the terminal state
    Then it reflects the working directory and environment of the most recent invocation only

  Scenario: Terminal state is absent before any terminal invocation
    Given a session exists with no terminal invocations recorded
    When the caller reads the terminal state
    Then no terminal state is returned

  Scenario: Empty history is not an error
    Given a session exists with no invocations recorded
    When the caller reads the terminal history
    Then an empty history is returned

  Scenario: Recording for an unknown session fails
    When the caller records a terminal invocation for a session id that does not exist
    Then a SessionNotFoundError is raised

  Scenario: Reading history for an unknown session fails
    When the caller reads the terminal history for a session id that does not exist
    Then a SessionNotFoundError is raised

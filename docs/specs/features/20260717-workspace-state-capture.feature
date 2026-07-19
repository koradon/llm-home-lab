Feature: Workspace state capture pipeline
  The orchestrator stores a caller-supplied snapshot of a session's coding workspace (branch,
  diff, open files, test status), bounded to a predictable size, so switching models never
  loses that context.

  # Related spec: docs/specs/20260717-workspace-state-capture.md

  Scenario: Capturing and reading a workspace snapshot round-trips
    Given a session exists with no workspace snapshot yet
    When the caller captures branch "main", a git diff, and two open files for that session
    Then reading the workspace snapshot returns the same branch, diff, and open files

  Scenario: Reading before any capture returns nothing
    Given a session exists with no workspace snapshot yet
    When the caller reads the workspace snapshot for that session
    Then no snapshot is returned

  Scenario: Capturing again replaces the previous snapshot
    Given a session has a captured workspace snapshot on branch "main"
    When the caller captures a new snapshot on branch "feature/x" for the same session
    Then reading the workspace snapshot returns branch "feature/x"
    And the previous snapshot is no longer returned

  Scenario: An oversized diff is truncated, not rejected
    Given a session exists with no workspace snapshot yet
    When the caller captures a git diff longer than the configured maximum
    Then reading the workspace snapshot returns a diff truncated to that maximum
    And the snapshot reports that the diff was truncated

  Scenario: An oversized open files list is truncated, not rejected
    Given a session exists with no workspace snapshot yet
    When the caller captures more open files than the configured maximum
    Then reading the workspace snapshot returns at most that maximum number of open files
    And the snapshot reports that the open files list was truncated

  Scenario: Capturing without test status is valid
    Given a session exists with no workspace snapshot yet
    When the caller captures a snapshot with no test status
    Then reading the workspace snapshot reports no test status

  Scenario: Capturing for an unknown session fails
    When the caller captures a workspace snapshot for a session id that does not exist
    Then a SessionNotFoundError is raised

  Scenario: Reading an unknown session fails
    When the caller reads the workspace snapshot for a session id that does not exist
    Then a SessionNotFoundError is raised

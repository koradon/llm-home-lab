Feature: Session manager core
  The orchestrator persists session state independently of any model backend, so switching
  backends or restarting never loses conversation history.

  # Related spec: docs/specs/20260717-session-manager-core.md

  Scenario: New session has no messages and no summary
    When a new session is created
    Then reading the session returns no messages and no summary

  Scenario: Messages are read back in append order
    Given a session exists
    When messages are appended in order "Hi", "Hello", "How are you?"
    Then reading the session returns those messages in the same order
    And the session summary is still absent

  Scenario: Summarizing replaces covered messages with a summary
    Given a session with 3 appended messages
    When the caller summarizes the session covering all 3 messages
    Then reading the session returns the summary
    And reading the session returns no messages older than the summary

  Scenario: Trimming after a summary deletes covered messages
    Given a session with 3 appended messages summarized covering all 3
    When the session is trimmed
    Then the trim result reports 3 messages deleted

  Scenario: Trimming a session with no summary is a no-op
    Given a session with 2 appended messages and no summary
    When the session is trimmed
    Then the trim result reports 0 messages deleted

  Scenario: Appending to an unknown session fails
    When a message is appended to a session id that does not exist
    Then a SessionNotFoundError is raised

  Scenario: Reading an unknown session fails
    When an unknown session id is read
    Then a SessionNotFoundError is raised

  Scenario: Summarizing beyond the highest message sequence fails
    Given a session with 2 appended messages
    When the caller summarizes the session covering a sequence number higher than any message
    Then an InvalidSummaryError is raised
    And no summary is stored for the session

  Scenario: Session state survives a restart
    Given a session with appended messages and a stored summary, written by one SessionManager
    When a new SessionManager is constructed against the same store path
    Then it reads back the same messages and summary

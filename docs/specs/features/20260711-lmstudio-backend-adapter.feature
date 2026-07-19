Feature: LM Studio backend adapter
  The gateway can dispatch chat completions to a real LM Studio host with classified failures.

  # Related spec: docs/specs/20260711-lmstudio-backend-adapter.md

  Scenario: Successful non-streaming completion
    Given a configured LM Studio host that responds successfully
    When the adapter completes a chat request
    Then it returns a BackendResponse with content, finish reason, and token usage

  Scenario: Successful streaming completion
    Given a configured LM Studio host that streams a completion
    When the adapter streams a chat request
    Then it yields a BackendChunk per upstream chunk
    And the stream ends when the upstream stream ends

  Scenario: Request times out and retries are exhausted
    Given a configured LM Studio host that always times out
    When the adapter completes a chat request
    Then it retries up to the configured maximum
    And it raises BackendTimeoutError

  Scenario: Connection fails and retries are exhausted
    Given a configured LM Studio host that is unreachable
    When the adapter completes a chat request
    Then it retries up to the configured maximum
    And it raises BackendConnectionError

  Scenario: Backend returns a non-2xx response
    Given a configured LM Studio host that returns HTTP 500
    When the adapter completes a chat request
    Then it raises BackendResponseError immediately without retrying
    And the error carries the upstream status code

  Scenario: Transient failure recovers within the retry budget
    Given a configured LM Studio host that fails once then succeeds
    When the adapter completes a chat request
    Then it returns a successful BackendResponse without exhausting retries

Feature: OpenAI-compatible chat completions gateway
  Agents call one stable endpoint and get OpenAI-shaped responses regardless of the backend.

  # Related spec: docs/specs/20260711-openai-compatible-api-gateway.md

  Scenario: Valid non-streaming request is served
    Given a configured LM Studio backend is healthy
    When a client posts a valid chat completion request with "stream" false
    Then the response has HTTP status 200
    And the response body matches the OpenAI chat completion response shape

  Scenario: Valid streaming request is served
    Given a configured LM Studio backend is healthy
    When a client posts a valid chat completion request with "stream" true
    Then the response is delivered as a sequence of server-sent event chunks
    And the stream ends with a "[DONE]" marker

  Scenario: Missing messages field is rejected
    When a client posts a chat completion request with no "messages" field
    Then the response has HTTP status 400
    And the response body matches the OpenAI error envelope shape
    And no request is forwarded to any backend

  Scenario: Empty messages array is rejected
    When a client posts a chat completion request with an empty "messages" array
    Then the response has HTTP status 400
    And the response body matches the OpenAI error envelope shape

  Scenario: Malformed JSON body is rejected
    When a client posts a request body that is not valid JSON
    Then the response has HTTP status 400
    And the response body matches the OpenAI error envelope shape

  Scenario: Backend timeout is surfaced as a gateway error
    Given the configured LM Studio backend does not respond within the timeout
    When a client posts a valid chat completion request
    Then the response has an HTTP 5xx status
    And the response body matches the OpenAI error envelope shape

  Scenario: Unrecognized top-level fields are tolerated
    When a client posts a valid chat completion request with an extra unknown field
    Then the response has HTTP status 200

  Scenario: Eight disconnected callers return all admitted slots
    Given a healthy fake backend accepts eight silent inference exchanges
    And all eight node slots are occupied
    When all eight inbound clients disconnect or reach their client timeout
    Then all backend requests and response sockets stop
    And the node in-flight count returns to zero with one release per admission

  Scenario: Connected silent inference has a finite bound
    Given a client stays connected to a silent backend with a configured read-gap ceiling
    When the ceiling expires before response headers
    Then the gateway returns 504 with error code "backend_timeout"
    And the backend response closes before its slot is released

  Scenario: Slow progressing inference outlasts its gap budget
    Given the backend emits bytes more often than the configured gap ceiling
    When the total response duration exceeds the ceiling
    Then the response succeeds with its full content and usage

  Scenario: A streaming response is closed before iteration starts
    Given a streaming request has acquired a host slot
    When its body iterator is closed before the first iteration
    Then the host slot is released exactly once
    And closing the body iterator again does not decrement capacity

  Scenario: A disconnected queue waiter is removed
    Given all host slots are occupied and another request is queued
    When that queued caller disconnects
    Then its queue entry is removed without acquiring a host slot

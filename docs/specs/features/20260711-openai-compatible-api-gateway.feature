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

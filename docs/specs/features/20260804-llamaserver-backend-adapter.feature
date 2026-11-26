Feature: llama-server backend adapter
  The gateway can dispatch chat completions to a real llama-server host with classified failures.

  # Related spec: docs/specs/20260804-llamaserver-backend-adapter.md

  Scenario: Successful non-streaming completion
    Given a configured llama-server host that responds successfully
    When the adapter completes a chat request
    Then it returns a BackendResponse with content, finish reason, and token usage

  Scenario: Successful streaming completion
    Given a configured llama-server host that streams a completion
    When the adapter streams a chat request
    Then it yields a BackendChunk per upstream chunk
    And the stream ends when the upstream stream ends

  Scenario: Request times out and retries are exhausted
    Given a configured llama-server host that always times out
    When the adapter completes a chat request
    Then it retries up to the configured maximum
    And it raises BackendTimeoutError

  Scenario: Connection fails and retries are exhausted
    Given a configured llama-server host that is unreachable
    When the adapter completes a chat request
    Then it retries up to the configured maximum
    And it raises BackendConnectionError

  Scenario: Backend returns a non-2xx response
    Given a configured llama-server host that returns HTTP 500
    When the adapter completes a chat request
    Then it raises BackendResponseError immediately without retrying
    And the error carries the upstream status code

  Scenario: Transient failure recovers within the retry budget
    Given a configured llama-server host that fails once then succeeds
    When the adapter completes a chat request
    Then it returns a successful BackendResponse without exhausting retries

  Scenario: Health check uses the native health endpoint
    Given a configured llama-server host
    When the adapter checks health
    Then it calls GET /health rather than proxying through a chat or model endpoint

  Scenario: Model is forwarded unchanged
    Given a configured llama-server host with no alias configuration
    When the adapter completes a chat request for a given model
    Then the request forwarded to llama-server uses that exact model name

  Scenario: Load probe reports busy when a slot is processing
    Given a configured llama-server host with one busy slot and one idle slot
    When the load probe reads GET /slots
    Then it reports status "busy" with a queued count of 1

  Scenario: Load probe degrades gracefully when /slots is disabled
    Given a configured llama-server host with /slots disabled
    When the load probe reads GET /slots
    Then it reports the load as unavailable rather than raising an error

  Scenario: Router node probes /models and /slots?model= for the loaded model
    Given a registered llama-server node with swap_mode "router"
    And the router reports one loaded model and one unloaded model on GET /models
    When the orchestrator probes the node
    Then it calls GET /slots?model=<loaded> instead of parameterless GET /slots
    And GET /v1/nodes reports external_load available with total_slots and tasks/h/slot
    And GET /v1/nodes exposes each router model and its state

  Scenario: Router list_models reports only servable models
    Given a llama-server backend in router mode with loaded, unloaded, loading, and failed models
    When list_models is called
    Then it queries GET /models and returns only loaded or sleeping models

  Scenario: Broken model on a router node does not take the node offline
    Given a registered llama-server router node serving a healthy model and a broken model
    When requests to the broken model fail repeatedly
    Then the node status remains online
    And GET /v1/capacity?model=<broken> returns 0
    And GET /v1/capacity?model=<healthy> still reports the node's capacity
    And requests to the healthy model continue to succeed

  Scenario: Request for a loading model returns 503 model_loading with Retry-After
    Given a registered llama-server router node whose model is currently loading
    When a chat completion arrives for that model
    Then the gateway returns HTTP 503 with error code "model_loading" and a Retry-After header
    And no failure is recorded against the node or model

Feature: Generation parameter forwarding
  Clients control temperature, token cap, chat template kwargs and response format per request.

  # Related spec: docs/specs/20261009-generation-parameter-forwarding.md

  Scenario: Parameters set by the client reach a llama-server backend
    Given a chat request with temperature, max_tokens, chat_template_kwargs and response_format
    When the llama-server backend dispatches it
    Then the backend payload contains each of them with the client's exact values

  Scenario: Parameters the client did not set are omitted
    Given a chat request that sets none of the generation parameters
    When the llama-server backend dispatches it
    Then the backend payload is identical to the payload sent before this feature
    And no generation parameter is sent as null

  Scenario: LM Studio receives only the standard OpenAI fields
    Given a chat request with temperature, max_tokens, response_format and chat_template_kwargs
    When the LM Studio backend dispatches it
    Then the payload contains temperature, max_tokens and response_format
    And it does not contain chat_template_kwargs

  Scenario: Invalid values are rejected before dispatch
    Given a chat request with max_tokens 0, or temperature outside 0 to 2, or a non-object chat_template_kwargs
    When the gateway receives it
    Then it responds 400 with an invalid_request_error envelope
    And no backend is called

  Scenario: Streaming requests carry the parameters too
    Given a streaming chat request with generation parameters
    When the gateway dispatches it
    Then the backend request carries the same parameters as a non-streaming one

---
title: M1 - Core Orchestrator Foundation
description: Establish a single OpenAI-compatible API gateway and baseline project
  architecture for the local LLM orchestrator.
state: open
number: 1
---

# M1 - Core Orchestrator Foundation

Goal: deliver a minimal but working orchestrator that exposes one stable API endpoint and can forward requests to at least one LM Studio backend.

## Exit Criteria

- OpenAI-compatible chat/completions endpoint is available.
- Backend adapter can call a local LM Studio instance.
- Health check endpoint reports orchestrator and backend status.
- Basic observability (request logs, latency, error codes) is present.

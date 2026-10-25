---
name: real-llm-node-lease
description: >-
  Load when an agent needs to check something against a real local LLM through
  llm-home-lab. Use llm-home-lab-run so a node is leased out of ordinary routing.
---

# Real LLM node lease

To test against a real LLM, wrap the command in `llm-home-lab-run`.
The wrapper reserves a node, exports `OPENAI_BASE_URL`, `ORCHESTRATOR_BASE_URL`, `LLM_HOME_LAB_NODE_LEASE`, and `LLM_HOME_LAB_LEASE_HEADER`, heartbeats the lease, and releases it when the command exits.
Send `X-LLM-Node-Lease` (the value of `LLM_HOME_LAB_LEASE_HEADER`) with the value of `LLM_HOME_LAB_NODE_LEASE` on every orchestrator request, and keep using the orchestrator base URL.

Never switch a node off by hand.
Never hold a node longer than the command you are running.
Never use a leased node for ordinary tests that can use fakes.

`--node <host_id>` picks a node. `--model <name>` picks one that serves that model.
A refused reservation exits 2 and names who holds which node. An unreachable orchestrator exits 3.

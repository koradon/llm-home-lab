---
name: real-llm-node-lease
description: >-
  Load when an agent needs to check something against a real local LLM through
  llm-home-lab. Use llm-home-lab-run so a node is leased out of ordinary routing.
  The wrapped command is unchanged and only needs the exported base URL.
---

# Real LLM node lease

Wrap the check in `llm-home-lab-run`. It reserves one node, takes that node out of ordinary routing, heartbeats the lease, and releases the node when the command exits. The command keeps talking to the orchestrator. Do not point it at a node address, and do not edit the client so it can send a custom header.

Do not install the wrapper with `uv tool` or `pipx`. From any other repository's worktree, run it out of the llm-home-lab checkout the brief names as `LLM_HOME_LAB_REPO`:

```bash
uv run --project "$LLM_HOME_LAB_REPO" -- llm-home-lab-run --node "$NODE" -- <command>
```

`--model <name>` reserves a healthy node that serves that model when the host id does not matter. `--node <host_id>` reserves that host.

The wrapper finds the orchestrator in the environment it inherits:

- `ORCHESTRATOR_BASE_URL` is the origin (`http://localhost:8080`) or that same URL already ending in `/v1`. Pass `--base-url` only when this variable is unset or points at the wrong orchestrator.
- `ORCHESTRATOR_API_KEY` is the API key. Pass `--api-key` only when it is not already set. The command keeps sending that key the way it already does.

After the reservation, the command's `ORCHESTRATOR_BASE_URL` and `OPENAI_BASE_URL` are the same leased base. Both end in `/v1` and contain `/lease/<token>`. Use that value as the client base. A client that posts `{ORCHESTRATOR_BASE_URL}/chat/completions` and reads `{ORCHESTRATOR_BASE_URL}/capacity` is served by the reserved node. Do not append another `/v1`. Do not send `X-LLM-Node-Lease` unless the client already does. A client that sends that header to the normal origin still reaches the reserved node.

Never switch a node off by hand. Never hold a node longer than the command you are running. Never use a leased node for a check that can use fakes.

A refused reservation exits 2 and names who holds which node. An unreachable orchestrator exits 3. The command's own exit code is passed through. SIGINT returns 130. A lease that ends while the command is running exits 4.

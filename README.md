# llm-home-lab

Local LLM orchestrator that exposes a single OpenAI-compatible API endpoint
for agents and routes requests across multiple local model hosts.

## Project Vision

This project aims to build a local control plane for home-lab LLM
infrastructure:

- one stable API for agents (`/v1` OpenAI-compatible),
- many local backends (for example LM Studio on multiple machines),
- stateless model workers,
- stateful orchestration layer that owns memory, tools, and routing decisions.

In short: "Kubernetes for home LLMs", but focused on agent workflows.

## Core Concepts

- **Session Manager**: persists conversation state, summaries, and decisions.
- **Workspace State**: tracks repository and runtime context for coding tasks.
- **Tool State**: keeps terminal/filesystem/tool continuity independent of model.
- **Routing Engine**: chooses the best model based on availability, load, and
  context constraints.

## High-Level Architecture

```text
Agent (OpenCode / other)
        |
        v
http://llm.home:8080/v1
        |
        v
Local LLM Orchestrator
        |
  +-----+-----+-----+
  |           |     |
LM Studio   LM Studio   LM Studio
MacBook     Windows     Linux
```

## Running

Deployment model isn't fully decided yet (see
[docs/ideas/deployment-model.md](docs/ideas/deployment-model.md)) — both paths below work
today.

**As an installed package (uv):**

```bash
uv sync
LMSTUDIO_BASE_URL=http://localhost:1234 uv run llm-home-lab
```

**As a container:**

```bash
docker compose up --build
```

The default `docker-compose.yml` points `LMSTUDIO_BASE_URL` at `host.docker.internal`, since
LM Studio runs natively on the host, not in a container.

Either way, the gateway listens on `:8080` (`ORCHESTRATOR_PORT` to override) with
`/v1/chat/completions`, `/health/live`, and `/health/ready`.

## Roadmap

Roadmap is managed in `.plan` and synced to GitHub issues/milestones via
Planhub.

Current milestones:

1. Core Orchestrator Foundation
2. Stateful Session and Tool Context
3. Intelligent Routing and Reliability
4. Production Hardening and Multi-Node Operations

## Planning and GitHub Sync

- Planning source of truth: `.plan/`
- Sync command:

```bash
planhub sync
```

- Safe validation before writing:

```bash
planhub sync --dry-run --compact
```

## Current Status

This repository currently contains project concept and planning artifacts.
Implementation code is expected to be added incrementally following milestone
issues in `.plan/`.

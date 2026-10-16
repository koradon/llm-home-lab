# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- Correct entries that work proves wrong; add new ones only by deliberate maintainer choice, never as routine task output.

## What this is

A local LLM orchestrator: one OpenAI-compatible API endpoint (`/v1/chat/completions`) that routes
requests across local model backends (LM Studio, llama.cpp's `llama-server`).

## Commands

- Install dependencies: `uv sync --dev`
- Run tests: `uv run pytest --cov=llm_home_lab`
- Lint: `uv run ruff check .`
- Format check: `uv run ruff format --check .`
- Type check: `uv run mypy src`

## Documentation conventions

This repo uses adrlane documentation-as-code; see `docs/llm/AGENT_PROTOCOL.md` for the contract
on where and how to write ideas, specs, plans, and ADRs under `docs/`.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.

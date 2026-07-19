# Use Python as the implementation language for the orchestrator

## Status

accepted

## Context and Problem Statement

The orchestrator is primarily an I/O-bound control plane: it proxies HTTP requests to local
model backends (LM Studio), performs health checks, tracks session/workspace/tool state, and
applies routing policy. It is not compute-heavy — the model inference itself runs in the
backends, not in the orchestrator. We need a language that lets us move quickly through the
M1–M4 milestone sequence (see [orchestrator-program plan](../plans/20260711-orchestrator-program.md))
while keeping the door open for future performance work.

## Considered Options

- Python (FastAPI/httpx/pydantic)
- Rust (e.g. axum/tokio)

## Decision Outcome

Chosen option: "Python", because the workload is I/O-bound rather than CPU-bound, so Python's
performance ceiling is unlikely to be the bottleneck for proxying, health checks, and routing
logic. The team has strong existing Python experience and none in Rust, and FastAPI/pydantic
map cleanly onto the OpenAI-compatible API contract validation needed for M1. Given the size of
the roadmap, implementation velocity — especially to ship M1 — outweighs the raw performance
and single-binary deployment benefits Rust would offer.

### Consequences

- Good, because the team can start implementing immediately without a language learning curve.
- Good, because FastAPI + pydantic give schema validation and OpenAPI-compatible request/response
  handling largely out of the box.
- Bad, because Python's per-request overhead and packaging/deployment story are weaker than
  Rust's, which may matter more once M4 (multi-node scheduling, production hardening) is
  reached.
- Bad, because the GIL constrains true CPU-bound parallelism, relevant if routing/scoring logic
  (M3) becomes computationally heavier than expected.
- Mitigation: if a specific component (e.g. the M3 routing engine) becomes a proven performance
  bottleneck, it can be reimplemented in Rust and exposed via a well-defined boundary (subprocess,
  FFI, or separate service) without rewriting the whole orchestrator.

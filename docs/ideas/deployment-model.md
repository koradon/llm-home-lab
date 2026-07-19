# Orchestrator deployment model

## Status

draft

## Problem

The orchestrator needs to run as a long-lived network service somewhere in the home lab, but
we haven't decided how it's packaged and started: as a container, as an installed native
package, or something else. Getting this wrong later means redoing packaging/ops work once
M4 (multi-node, production hardening) needs it to be reliably deployed across hosts.

## Audience / Value

The operator (running this at home) needs a low-friction way to start, restart on boot, and
upgrade the orchestrator, on whichever machine ends up hosting the control plane.

## Solution shape (not final)

Support both paths in parallel for now, without committing:

- Keep the orchestrator runnable as an installed Python package (`uv run orchestrator` /
  `uv tool install`, as it is today via `[project.scripts]`).
- Add a `Dockerfile` (and optionally `docker-compose.yml`) so it can also run as a container.

Decide the actual target deployment model when M4 (production hardening / multi-node
operations) needs it — at that point we'll know more about where the control plane needs to
live relative to the LM Studio hosts it talks to.

## Options

- **Docker**: long-lived service in a container on a dedicated host (NAS, mini-server).
  Restart-on-boot, isolation from the host OS, consistent deploy regardless of what else runs
  on that machine. LM Studio hosts stay native apps on their own machines; the orchestrator
  reaches them over the network.
- **Installed native package**: `uv tool install` / pip, run via systemd (Linux) or launchd
  (macOS). Simpler when the orchestrator shares a machine with one of the LM Studio hosts, no
  container networking to reason about (e.g. reaching `localhost:1234` directly).
- **Both, decide later**: keep it a runnable Python package as now, add a Dockerfile alongside
  it, and pick the primary path once M4's actual hosting constraints are known. This is the
  current direction.

## Constraints / Appetite

Low effort for now — a working `Dockerfile` alongside the existing package entry point, no
orchestration (compose/k8s) commitment yet.

## Rabbit holes

- Docker networking to a LM Studio instance running natively on the same host (e.g.
  `host.docker.internal` on macOS vs. host networking on Linux) — only matters if orchestrator
  and a LM Studio host ever end up co-located, which the multi-node model discourages by
  design.
- Building a "proper" release pipeline (multi-arch images, registry, versioning) before there's
  a second real deployment target is premature — avoid it until M4.

## No-gos

- No Kubernetes/orchestration platform commitment at this stage — out of scope until real
  multi-node needs (M4) are understood.
- No decision yet on where the container image is published (if at all); a locally built image
  is enough for now.

## Related

- Plan: [orchestrator-program](../plans/20260711-orchestrator-program.md) (M4 — production hardening and
  multi-node operations, where this gets decided for real)
- ADR: [0001-python-as-implementation-language](../adr/0001-python-as-implementation-language.md)

## Open Questions

- Does the orchestrator ever run on the same host as an LM Studio instance, or is it always a
  separate control-plane host? This shapes whether Docker networking is a real concern.
- Should the Dockerfile target a slim/distroless base for a smaller image, or prioritize build
  simplicity while the project is still pre-M4?

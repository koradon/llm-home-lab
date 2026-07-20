FROM python:3.12-slim AS base

FROM base AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src ./src
RUN uv sync --frozen --no-dev

FROM base
COPY --from=builder /app /app
ENV PATH="/app/.venv/bin:$PATH"
WORKDIR /app

# Optional: install LM Studio's `lms` CLI for external node load visibility
# (GET /v1/nodes external_load field, TUI ext_load column — see ADR-0005).
# Everything else works without it; uncomment if you want that signal.
# RUN apt-get update && apt-get install -y --no-install-recommends curl \
#     && curl -fsSL https://lmstudio.ai/cli/install.sh | bash \
#     && rm -rf /var/lib/apt/lists/*

EXPOSE 8080
ENTRYPOINT ["llm-home-lab"]

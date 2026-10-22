# Same image for local Docker and Amazon Bedrock AgentCore Runtime (linux/arm64).
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    DOCKER_CONTAINER=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependencies first so they are cached independently of agent definitions.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --extra agentcore-otel

# Agent definitions are part of the artifact; environment-specific values come from env vars.
COPY runtime.toml ./
COPY agents ./agents

RUN useradd --create-home --uid 1000 runtime && chown -R runtime:runtime /app
USER runtime

EXPOSE 8080
CMD ["opentelemetry-instrument", "python", "-m", "strands_runtime.server"]

# Same image for local Docker and Amazon Bedrock AgentCore Runtime (linux/arm64).
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

# Optional dependency groups to install; add model-provider extras your agents need.
ARG EXTRAS="agentcore-otel openai"

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    DOCKER_CONTAINER=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependencies first so they are cached independently of agent definitions.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY packages/client ./packages/client
RUN uv sync --frozen --no-default-groups $(for e in $EXTRAS; do printf -- '--extra %s ' "$e"; done)

# Agent definitions are part of the artifact; environment-specific values come from env vars.
COPY runtime.toml ./
COPY agents ./agents
COPY deploy ./deploy

RUN useradd --create-home --uid 1000 runtime && chown -R runtime:runtime /app
USER runtime

# AgentCore's HTTP contract: 0.0.0.0:8080 (DOCKER_CONTAINER=1 selects the host). The port flag wins over runtime.toml.
EXPOSE 8080
CMD ["opentelemetry-instrument", "strands-runtime", "serve", "--port", "8080"]

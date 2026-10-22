"""``BedrockAgentCoreApp`` wiring.

The same app serves local development, Docker and AgentCore Runtime: AgentCore's SDK implements the
service contract (ports, ``/ping``, SSE framing, session header parsing), this module only maps the
runtime's payload to an invocation.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from bedrock_agentcore import BedrockAgentCoreApp, RequestContext
from pydantic import ValidationError
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from strands_runtime.errors import UnknownAgentError
from strands_runtime.events import ErrorEvent
from strands_runtime.invoke import InvocationRequest
from strands_runtime.runtime import Runtime

logger = logging.getLogger(__name__)


def create_app(runtime: Runtime) -> BedrockAgentCoreApp:
    app = BedrockAgentCoreApp()

    @app.entrypoint
    async def invocations(payload: Any, context: RequestContext) -> Any:
        try:
            request = InvocationRequest.model_validate(payload)
        except ValidationError as exc:
            detail = "; ".join(
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors(include_url=False)
            )
            raise HTTPException(status_code=400, detail=f"invalid payload: {detail}") from exc

        try:
            runtime.invoker.resolve_agent_name(request)
        except UnknownAgentError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        header_session_id = context.session_id if context is not None else None

        if request.stream:
            return _sse(runtime, request, header_session_id)

        result = await runtime.invoker.invoke(request, session_id=header_session_id)
        if isinstance(result, ErrorEvent):
            return JSONResponse(result.model_dump(mode="json"), status_code=500)
        return result.model_dump(mode="json")

    return app


async def _sse(runtime: Runtime, request: InvocationRequest, session_id: str | None) -> AsyncIterator[dict[str, Any]]:
    """Each yielded mapping becomes one ``data: {...}`` SSE frame (framing done by the AgentCore SDK)."""
    async for event in runtime.invoker.stream(request, session_id=session_id):
        yield event.model_dump(mode="json")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    runtime = Runtime.load()
    app = create_app(runtime)
    logger.info("serving agents: %s", ", ".join(runtime.registry.published))
    app.run(port=runtime.config.server.port, host=runtime.config.server.host)

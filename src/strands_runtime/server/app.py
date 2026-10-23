"""``BedrockAgentCoreApp`` の配線。

ローカル開発、Docker、任意のコンテナ基盤、AgentCore Runtime のすべてで同じアプリを使う。
HTTP 契約（ポート、``/ping``、SSE の枠組み、セッションヘッダーの解釈）は AgentCore の SDK が実装しているので、
このモジュールはペイロードを呼び出しに対応付けるだけ。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from bedrock_agentcore import BedrockAgentCoreApp, RequestContext
from pydantic import ValidationError
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from strands_runtime.errors import SessionBusyError, UnknownAgentError
from strands_runtime.events import ErrorEvent
from strands_runtime.invoke import InvocationRequest
from strands_runtime.logging_setup import configure_logging
from strands_runtime.runtime import Runtime

logger = logging.getLogger(__name__)


def create_app(runtime: Runtime) -> BedrockAgentCoreApp:
    """``POST /invocations`` のエントリポイントを持つアプリを作る。"""
    app = BedrockAgentCoreApp()

    # 第 2 引数の名前が ``context`` のときだけ SDK が RequestContext（セッション ID、許可されたヘッダー）を渡す。
    @app.entrypoint
    async def invocations(payload: Any, context: RequestContext) -> Any:
        try:
            request = InvocationRequest.model_validate(payload)
        except ValidationError as exc:
            detail = "; ".join(
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors(include_url=False)
            )
            raise HTTPException(status_code=400, detail=f"invalid payload: {detail}") from exc

        # エージェント名の解決はストリームを始める前に行い、未知の名前は 404 で返す。
        try:
            runtime.invoker.resolve_agent_name(request)
        except UnknownAgentError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        header_session_id = context.session_id if context is not None else None

        if request.stream:
            stream = runtime.invoker.stream(request, session_id=header_session_id)
            # 最初のイベントを取り出すまでに SessionBusyError が出るので、SSE を始める前に 409 を返せる。
            try:
                first = await anext(stream)
            except SessionBusyError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return _sse(first, stream)

        try:
            result = await runtime.invoker.invoke(request, session_id=header_session_id)
        except SessionBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if isinstance(result, ErrorEvent):
            return JSONResponse(result.model_dump(mode="json"), status_code=500)
        return result.model_dump(mode="json")

    return app


async def _sse(first: Any, rest: AsyncIterator[Any]) -> AsyncIterator[dict[str, Any]]:
    """yield した辞書 1 つが SSE の ``data: {...}`` フレーム 1 つになる（枠組みは AgentCore SDK が付ける）。"""
    yield first.model_dump(mode="json")
    async for event in rest:
        yield event.model_dump(mode="json")


def run_app(
    app: BedrockAgentCoreApp,
    runtime: Runtime,
    *,
    port: int | None = None,
    host: str | None = None,
    verbose: bool = False,
) -> None:
    """ログ（標準エラー出力と、あれば ``[server] log_file``）を設定し、停止されるまでサーバーを動かす。

    ``log_config=None`` で uvicorn 独自のハンドラー設定を止め、uvicorn のログも同じルートロガー
    （したがって同じファイル）に流す。
    """
    log_file = runtime.config.server.log_file
    configure_logging(
        logging.DEBUG if verbose else logging.INFO,
        log_file=(runtime.base_dir / log_file).resolve() if log_file else None,
    )
    logger.info("serving agents: %s", ", ".join(runtime.registry.published))
    if log_file:
        logger.info("log file: %s", (runtime.base_dir / log_file).resolve())
    app.run(
        port=port or runtime.config.server.port,
        host=host or runtime.config.server.host,
        log_config=None,
        log_level="debug" if verbose else "info",
    )


def main() -> None:
    """``python -m strands_runtime.server`` の入口。"""
    runtime = Runtime.load()
    run_app(create_app(runtime), runtime)

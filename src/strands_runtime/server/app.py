"""``BedrockAgentCoreApp`` の配線。

ローカル開発、Docker、任意のコンテナ基盤、AgentCore Runtime のすべてで同じアプリを使う。
HTTP 契約（ポート、``/ping``、SSE の枠組み、セッションヘッダーの解釈）は AgentCore の SDK が実装しているので、
このモジュールはペイロードを呼び出しに対応付けるだけ。
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio.to_thread
from bedrock_agentcore import BedrockAgentCoreApp, RequestContext
from pydantic import ValidationError
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from strands_runtime.errors import (
    CapacityError,
    InvalidRequestError,
    JobsDisabledError,
    SessionBusyError,
    UnknownAgentError,
    UnknownJobError,
)
from strands_runtime.invoke import InvocationRequest
from strands_runtime.logging_setup import configure_logging
from strands_runtime.runtime import CONFIG_ENV_VAR, Runtime
from strands_runtime.server.execution import ExecutionLoop
from strands_runtime_client.events import ErrorEvent, dump_event

logger = logging.getLogger(__name__)


class _AgentCoreBusyTracker:
    """実行中のジョブを SDK の非同期タスクとして登録し、``/ping`` を ``HealthyBusy`` にする。"""

    def __init__(self, app: BedrockAgentCoreApp) -> None:
        self._app = app

    def start(self, name: str) -> int:
        return self._app.add_async_task(name)

    def finish(self, handle: int) -> bool:
        return self._app.complete_async_task(handle)


def create_app(runtime: Runtime) -> BedrockAgentCoreApp:
    """``POST /invocations`` のエントリポイントを持つアプリを作る。"""

    execution = ExecutionLoop(runtime)

    @asynccontextmanager
    async def lifespan(_: Any) -> AsyncIterator[None]:
        if runtime.shared_store is not None:
            await asyncio.to_thread(runtime.shared_store.connect)
        limiter = anyio.to_thread.current_default_thread_limiter()
        original = limiter.total_tokens
        limiter.total_tokens = runtime.config.server.http_threads
        try:
            yield
        finally:
            try:
                await execution.shutdown()
            finally:
                limiter.total_tokens = original

    app = BedrockAgentCoreApp(lifespan=lifespan)
    tracker = _AgentCoreBusyTracker(app)
    runtime.jobs.busy_tracker = tracker

    # 第 2 引数の名前が ``context`` のときだけ SDK が RequestContext（セッション ID、許可されたヘッダー）を渡す。
    @app.entrypoint
    async def invocations(payload: Any, context: RequestContext) -> Any:
        execution.bind()
        try:
            request = InvocationRequest.model_validate(payload)
        except ValidationError as exc:
            detail = "; ".join(
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors(include_url=False)
            )
            raise HTTPException(status_code=400, detail=f"invalid payload: {detail}") from exc

        header_session_id = context.session_id if context is not None else None
        if runtime.config.session.backend == "agentcore":
            if not header_session_id:
                raise HTTPException(status_code=400, detail="agentcore requires the runtime session header")
            if request.session_id is not None and request.session_id != header_session_id:
                raise HTTPException(
                    status_code=400, detail="session_id must match the AgentCore runtime session header"
                )
        if request.mode in ("run", "job"):
            return await _execute(runtime, request, header_session_id)
        return await _observe_job(runtime, request, header_session_id)

    return app


async def _execute(runtime: Runtime, request: InvocationRequest, header_session_id: str | None) -> Any:
    """``mode = "run"``（同期実行）と ``mode = "job"``（受付して即返す）。"""
    # エージェント名の解決はストリームを始める前に行い、未知の名前は 404 で返す。
    try:
        runtime.invoker.resolve_agent_name(request)
    except UnknownAgentError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidRequestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        if request.mode == "job":
            return dump_event(await runtime.jobs.submit(request, session_id=header_session_id))
        if request.stream:
            stream = runtime.invoker.stream(request, session_id=header_session_id)
            # 最初のイベントを取り出すまでに SessionBusyError が出るので、SSE を始める前に 409 を返せる。
            first = await anext(stream)
            return _sse(first, stream)
        result = await runtime.invoker.invoke(request, session_id=header_session_id)
    except SessionBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except CapacityError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503, headers={"Retry-After": "1"})
    except JobsDisabledError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(result, ErrorEvent):
        return JSONResponse(dump_event(result), status_code=500)
    return dump_event(result)


async def _observe_job(runtime: Runtime, request: InvocationRequest, header_session_id: str | None) -> Any:
    """``status`` / ``follow`` / ``result`` / ``cancel``。セッション ID は必須で、``job_id`` 省略時は最新のジョブ。"""
    session_id = header_session_id or request.session_id
    if session_id is None:
        raise HTTPException(status_code=400, detail=f"session_id is required when mode is {request.mode!r}")
    jobs = runtime.jobs
    try:
        match request.mode:
            case "status":
                return dump_event(await jobs.status(session_id, request.job_id))
            case "result":
                return dump_event(await jobs.result(session_id, request.job_id))
            case "cancel":
                return dump_event(await jobs.cancel(session_id, request.job_id))
            case _:
                stream = jobs.follow(session_id, request.job_id, after=request.after)
                # 記録の読み込み（UnknownJobError）は最初のイベントを取り出す前に終わるので、SSE の前に 404 を返せる。
                # ``after`` が末尾以降で終端にも達していれば、フレームを出さずに閉じる。
                first = await anext(stream, None)
                return _sse(first, stream)
    except UnknownJobError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except JobsDisabledError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _sse(first: Any, rest: AsyncIterator[Any]) -> AsyncIterator[dict[str, Any]]:
    """yield した辞書 1 つが SSE の ``data: {...}`` フレーム 1 つになる（枠組みは AgentCore SDK が付ける）。"""
    try:
        if first is not None:
            yield dump_event(first)
        async for event in rest:
            yield dump_event(event)
    finally:
        aclose = getattr(rest, "aclose", None)
        if aclose is not None:
            await aclose()


def run_app(
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
    import uvicorn
    from uvicorn.supervisors import Multiprocess

    in_container = Path("/.dockerenv").exists() or bool(os.environ.get("DOCKER_CONTAINER"))
    bind_host = host or runtime.config.server.host or ("0.0.0.0" if in_container else "127.0.0.1")
    previous_config = os.environ.get(CONFIG_ENV_VAR)
    os.environ[CONFIG_ENV_VAR] = str(runtime.config_path)
    try:
        # Windowsでもspawnできるよう、構築済みアプリではなくインポート可能なファクトリを渡す。
        config = uvicorn.Config(
            "strands_runtime.server.app:configured_app",
            factory=True,
            port=port or runtime.config.server.port,
            host=bind_host,
            log_config=None,
            log_level="debug" if verbose else "info",
            access_log=verbose,
            workers=runtime.config.server.workers,
            backlog=runtime.config.server.backlog,
            timeout_keep_alive=runtime.config.server.timeout_keep_alive,
            timeout_graceful_shutdown=runtime.config.server.timeout_graceful_shutdown,
            timeout_worker_healthcheck=runtime.config.server.timeout_worker_healthcheck,
        )
        if config.workers == 1:
            uvicorn.Server(config).run()
        else:
            with config.bind_socket() as sock:
                # Windowsでは、未listenのソケットを共有して各子が同時にlistenすると10022になる。
                # 親でlistenを完了してから、Uvicornの標準supervisorへ渡す。
                sock.listen(config.backlog)
                Multiprocess(config, sockets=[sock]).run()
    finally:
        if previous_config is None:
            os.environ.pop(CONFIG_ENV_VAR, None)
        else:
            os.environ[CONFIG_ENV_VAR] = previous_config


def configured_app() -> BedrockAgentCoreApp:
    """各ワーカーが設定・モデル・MCP接続・イベントループを独立して初期化する。"""
    runtime = Runtime.load()
    log_file = runtime.config.server.log_file
    configure_logging(
        logging.DEBUG if logging.getLogger("uvicorn.error").isEnabledFor(logging.DEBUG) else logging.INFO,
        log_file=(runtime.base_dir / log_file).resolve() if log_file else None,
    )
    return create_app(runtime)


def main() -> None:
    """``python -m strands_runtime.server`` の入口。"""
    runtime = Runtime.load()
    run_app(runtime)

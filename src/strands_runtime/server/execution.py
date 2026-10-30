"""AgentCore SDK が所有する実行ループの設定と、同じループ上での終了処理。"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

from strands_runtime.runtime import Runtime


class ExecutionLoop:
    def __init__(self, runtime: Runtime) -> None:
        self._runtime = runtime
        self._loop: asyncio.AbstractEventLoop | None = None
        self._executor: ThreadPoolExecutor | None = None

    def bind(self) -> None:
        """SDK のエントリポイント内から呼ぶ。HTTP のイベントループとは別。"""
        loop = asyncio.get_running_loop()
        if self._loop is not None:
            if self._loop is not loop:
                raise RuntimeError("a runtime must execute invocations on one event loop")
            return
        self._executor = ThreadPoolExecutor(
            max_workers=self._runtime.config.server.io_threads, thread_name_prefix="runtime-io"
        )
        loop.set_default_executor(self._executor)
        self._loop = loop

    async def shutdown(self) -> None:
        async def finish() -> None:
            self._runtime.invoker.begin_shutdown()
            await self._runtime.jobs.shutdown()

        try:
            if self._loop is None or self._loop is asyncio.get_running_loop():
                await finish()
            else:
                # asyncio.Task / Condition は、それを作った実行ループ上で停止・通知する。
                await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(finish(), self._loop))
        finally:
            if self._executor is not None:
                self._executor.shutdown(wait=False, cancel_futures=True)

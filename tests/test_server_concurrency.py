from __future__ import annotations

import asyncio
import threading

import httpx
import pytest

from strands_runtime.server import create_app
from strands_runtime.server.app import run_app
from strands_runtime_client.events import ResultEvent
from tests.test_admission import limited_runtime


@pytest.mark.parametrize("mode,stream", [("run", False), ("run", True), ("job", True)])
async def test_http_overload_is_503_before_stream_and_control_requests_still_work(tmp_path, monkeypatch, mode, stream):
    runtime = limited_runtime(tmp_path, queued=0)
    entered, release = threading.Event(), threading.Event()

    async def execute(run):
        entered.set()
        await asyncio.to_thread(release.wait, 5)
        yield ResultEvent(agent="a", session_id=run.session.session_id, stop_reason="end_turn", text="ok")

    monkeypatch.setattr(runtime.invoker, "_execute", execute)
    app = create_app(runtime)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client,
    ):
        accepted = await client.post("/invocations", json={"prompt": "go", "mode": "job", "session_id": "busy"})
        assert accepted.status_code == 200
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            response = await client.post("/invocations", json={"prompt": "go", "mode": mode, "stream": stream})
            assert response.status_code == 503 and response.headers["retry-after"] == "1"
            assert response.headers["content-type"] == "application/json"
            assert "full" in response.json()["error"]
            duplicate = await client.post("/invocations", json={"prompt": "go", "session_id": "busy"})
            assert duplicate.status_code == 409
            assert (await client.get("/ping")).status_code == 200
            status = await client.post("/invocations", json={"mode": "status", "session_id": "busy"})
            assert status.json()["state"] == "running"
            cancelled = await client.post("/invocations", json={"mode": "cancel", "session_id": "busy"})
            assert cancelled.json()["state"] == "cancelling"
        finally:
            release.set()


async def test_http_queue_timeout_returns_503_and_releases_waiting_session(tmp_path, monkeypatch):
    runtime = limited_runtime(tmp_path, queued=1, timeout=0.03)
    entered, release = threading.Event(), threading.Event()

    async def execute(run):
        entered.set()
        await asyncio.to_thread(release.wait, 5)
        yield ResultEvent(agent="a", session_id=run.session.session_id, stop_reason="end_turn", text="ok")

    monkeypatch.setattr(runtime.invoker, "_execute", execute)
    app = create_app(runtime)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client,
    ):
        active = asyncio.create_task(client.post("/invocations", json={"prompt": "go", "stream": False}))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            payload = {"prompt": "go", "session_id": "waiting"}
            response = await client.post("/invocations", json=payload)
            assert response.status_code == 503 and "timed out" in response.json()["error"]
            release.set()
            assert (await active).status_code == 200
            assert (await client.post("/invocations", json=payload)).status_code == 200
        finally:
            release.set()
            await active


async def test_agentcore_requires_matching_routing_id_and_reports_background_work(tmp_path, monkeypatch):
    runtime = limited_runtime(tmp_path)
    runtime.config.session.backend = "agentcore"
    entered, release = threading.Event(), threading.Event()

    async def execute(run):
        entered.set()
        await asyncio.to_thread(release.wait, 5)
        yield ResultEvent(agent="a", session_id=run.session.session_id, stop_reason="end_turn", text="ok")

    monkeypatch.setattr(runtime.invoker, "_execute", execute)
    app = create_app(runtime)
    headers = {"X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": "agentcore-session"}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client,
    ):
        assert (await client.post("/invocations", json={"prompt": "go"})).status_code == 400
        mismatch = await client.post("/invocations", headers=headers, json={"prompt": "go", "session_id": "other"})
        assert mismatch.status_code == 400
        accepted = await client.post("/invocations", headers=headers, json={"prompt": "go", "mode": "job"})
        assert accepted.status_code == 200
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            assert (await client.get("/ping")).json()["status"] == "HealthyBusy"
            duplicate = await client.post("/invocations", headers=headers, json={"prompt": "go"})
            assert duplicate.status_code == 409
            cancelled = await client.post("/invocations", headers=headers, json={"mode": "cancel"})
            assert cancelled.status_code == 200
        finally:
            release.set()


async def test_io_pool_bounds_blocking_tools_without_serializing_async_requests(tmp_path, monkeypatch):
    runtime = limited_runtime(tmp_path, concurrent=4, queued=0)
    runtime.config.server.io_threads = 2
    release, entered = threading.Event(), threading.Event()
    lock = threading.Lock()
    running = peak = 0

    def blocking_io():
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
            if running == 2:
                entered.set()
        try:
            release.wait(5)
        finally:
            with lock:
                running -= 1

    async def execute(run):
        await asyncio.to_thread(blocking_io)
        yield ResultEvent(agent="a", session_id=run.session.session_id, stop_reason="end_turn", text="ok")

    monkeypatch.setattr(runtime.invoker, "_execute", execute)
    app = create_app(runtime)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client,
    ):
        tasks = [
            asyncio.create_task(client.post("/invocations", json={"prompt": "go", "stream": False})) for _ in range(4)
        ]
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            assert (await client.get("/ping")).status_code == 200
        finally:
            release.set()
            responses = await asyncio.gather(*tasks)
    assert all(r.status_code == 200 for r in responses) and peak == 2


def test_server_shutdown_cancels_job_on_its_own_loop(runtime):
    from starlette.testclient import TestClient

    from tests import fakes

    fakes.script(fakes.text("x" * 500))
    fakes.set_chunk_delay(0.01)
    try:
        with TestClient(create_app(runtime)) as client:
            accepted = client.post("/invocations", json={"mode": "job", "prompt": "go", "session_id": "shutdown"})
            assert accepted.status_code == 200
        # 別イベントループの HTTP lifespan から、実行ループのジョブを停止して保存できる。
        event = asyncio.run(runtime.jobs.status("shutdown"))
        assert event.state == "cancelled" and event.cancelled_by == "runtime"
    finally:
        fakes.set_chunk_delay(0)


def test_server_passes_tcp_settings_and_does_not_inherit_worker_count(runtime, monkeypatch):
    from unittest.mock import Mock

    runtime.config.server.backlog = 123
    runtime.config.server.timeout_keep_alive = 7
    runtime.config.server.timeout_graceful_shutdown = 9
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    runtime.config.server.workers = 2
    config = Mock()
    config.workers = 2
    config.backlog = 123
    socket = Mock()
    config.bind_socket.return_value.__enter__ = Mock(return_value=socket)
    config.bind_socket.return_value.__exit__ = Mock(return_value=False)
    build_config = Mock(return_value=config)
    supervisor = Mock()
    monkeypatch.setattr("uvicorn.Config", build_config)
    monkeypatch.setattr("uvicorn.supervisors.Multiprocess", supervisor)
    run_app(runtime)
    assert build_config.call_args.args == ("strands_runtime.server.app:configured_app",)
    assert build_config.call_args.kwargs["factory"] is True
    assert build_config.call_args.kwargs["workers"] == 2
    assert build_config.call_args.kwargs["backlog"] == 123
    assert build_config.call_args.kwargs["timeout_keep_alive"] == 7
    assert build_config.call_args.kwargs["timeout_graceful_shutdown"] == 9
    socket.listen.assert_called_once_with(123)
    supervisor.return_value.run.assert_called_once()


@pytest.mark.parametrize("concurrent", [1, 4, 16])
async def test_burst_of_48_requests_obeys_execution_limit(tmp_path, monkeypatch, concurrent):
    runtime = limited_runtime(tmp_path, concurrent=concurrent, queued=48, timeout=10)
    active = peak = 0
    first_wave = asyncio.Event()

    async def execute(run):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == concurrent:
            first_wave.set()
        try:
            # ディスク速度に依存せず、最初の実行枠がすべて埋まるまで保持する。
            await asyncio.wait_for(first_wave.wait(), 5)
            await asyncio.sleep(0.02)  # LLM応答待ちの代わり。外部APIは呼ばない。
            yield ResultEvent(agent="a", session_id=run.session.session_id, stop_reason="end_turn", text="ok")
        finally:
            active -= 1

    monkeypatch.setattr(runtime.invoker, "_execute", execute)
    app = create_app(runtime)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client,
    ):
        responses = await asyncio.gather(
            *(
                client.post("/invocations", json={"prompt": "go", "session_id": f"s-{index}", "stream": False})
                for index in range(48)
            )
        )
    assert all(response.status_code == 200 for response in responses)
    assert peak == concurrent and active == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_concurrent", 0),
        ("max_queued", -1),
        ("queue_timeout_seconds", 0),
        ("queue_timeout_seconds", float("inf")),
    ],
)
def test_invalid_execution_configuration_is_rejected(field, value):
    from pydantic import ValidationError

    from strands_runtime.definitions.schema import ExecutionSection

    with pytest.raises(ValidationError):
        ExecutionSection.model_validate({field: value})

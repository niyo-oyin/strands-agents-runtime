from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from contextlib import suppress

import httpx
import pytest

from strands_runtime.definitions.schema import ExecutionSection
from strands_runtime.errors import SessionBusyError
from strands_runtime.invoke.admission import AdmissionController
from tests.conftest import write_agent, write_runtime


async def test_separate_controllers_share_session_reservation_while_queued(tmp_path):
    config = ExecutionSection(max_concurrent=1, max_queued=1, queue_timeout_seconds=5)
    one = AdmissionController(config, sqlite_path=tmp_path / "execution.sqlite3")
    two = AdmissionController(config, sqlite_path=tmp_path / "execution.sqlite3")
    attempting = asyncio.Event()

    async def queued():
        attempting.set()
        async with one.acquire("same"):
            pass

    async with one.acquire("other"):
        task = asyncio.create_task(queued())
        await attempting.wait()
        for _ in range(100):
            try:
                async with two.acquire("same"):
                    pass
            except SessionBusyError:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("queued session was not reserved")
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    async with two.acquire("same"):
        pass


LOCK_HOLDER = """
import asyncio, sys
from pathlib import Path
from strands_runtime.definitions.schema import ExecutionSection
from strands_runtime.invoke.admission import AdmissionController
async def main():
    gate = AdmissionController(ExecutionSection(), sqlite_path=Path(sys.argv[1]) / "execution.sqlite3")
    async with gate.acquire("same"):
        print("locked", flush=True)
        await asyncio.to_thread(sys.stdin.readline)
asyncio.run(main())
"""


async def test_dead_worker_reservation_is_reclaimed(tmp_path):
    process = subprocess.Popen(
        [sys.executable, "-c", LOCK_HOLDER, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        assert await asyncio.wait_for(asyncio.to_thread(process.stdout.readline), 15) == "locked\n"
        gate = AdmissionController(ExecutionSection(), sqlite_path=tmp_path / "execution.sqlite3")
        with pytest.raises(SessionBusyError):
            async with gate.acquire("same"):
                pytest.fail("cross-process duplicate accepted")
        process.kill()
        await asyncio.to_thread(process.wait, 10)
        # Windowsのプロセス一覧への終了反映には短い遅れがあり得る。
        async with asyncio.timeout(3):
            while True:
                try:
                    async with gate.acquire("same"):
                        break
                except SessionBusyError:
                    await asyncio.sleep(0.01)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        process.stdin.close()
        process.stdout.close()
        process.stderr.close()


WORKER_MODEL = """
import asyncio, json, os
from strands.models import Model
class WorkerModel(Model):
    def __init__(self):
        self.config = {"model_id": "test/worker", "context_window_limit": 10000}
    def update_config(self, **kwargs):
        self.config.update(kwargs)
    def get_config(self):
        return self.config
    async def structured_output(self, *args, **kwargs):
        raise NotImplementedError
        yield
    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        prompt = messages[-1]["content"][0]["text"]
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockStart": {"start": {}}}
        yield {"contentBlockDelta": {"delta": {"text": json.dumps({"pid": os.getpid()})}}}
        if prompt == "hold":
            for _ in range(100):
                await asyncio.sleep(0.1)
                yield {"contentBlockDelta": {"delta": {"text": " "}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "end_turn"}}
        yield {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}, "metrics": {"latencyMs": 1}}}
def make_model():
    return WorkerModel()
"""


SERVER = """
import sys
from pathlib import Path
from strands_runtime.runtime import Runtime
from strands_runtime.server import run_app
import strands_runtime.jobs.manager as jobs
jobs.HEARTBEAT_SECONDS = 0.1
if __name__ == "__main__":
    run_app(Runtime.load(Path(sys.argv[1])))
"""


@pytest.fixture
def multiprocess_server(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    agent = write_agent(
        tmp_path / "agents", "a", model_table='[model]\nmodule = "./worker.py:make_model"', with_tool=False
    )
    (agent / "worker.py").write_text(WORKER_MODEL, encoding="utf-8")
    config = write_runtime(
        tmp_path,
        f"""default_agent = "a"
[server]
workers = 2
host = "127.0.0.1"
port = {port}
[session]
backend = "local"
[session.history]
backend = "local"
[execution]
max_concurrent = 1
max_queued = 0
""",
    )
    output_path = tmp_path / "server.log"
    launcher = tmp_path / "launch.py"
    launcher.write_text(SERVER, encoding="utf-8")
    with output_path.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(
            [sys.executable, str(launcher), str(config)],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=output,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        url = f"http://127.0.0.1:{port}"
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                try:
                    if httpx.get(url + "/ping", timeout=0.5).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if process.poll() is not None:
                    pytest.fail(output_path.read_text(encoding="utf-8"))
                time.sleep(0.1)
            else:
                pytest.fail("workers did not start: " + output_path.read_text(encoding="utf-8"))
            yield url
        finally:
            # このテストは実際のspawn/HTTP配送/プロセス間排他を検証する。
            # コンソールのないWindowsでも確実に片付ける。通常の終了処理は別テストで検証する。
            import psutil

            if process.poll() is None:
                children = psutil.Process(process.pid).children(recursive=True)
                process.kill()
                for child in children:
                    with suppress(psutil.NoSuchProcess):
                        child.kill()
                psutil.wait_procs(children, timeout=10)
                process.wait(timeout=10)


def test_two_uvicorn_workers_share_reservations_and_job_observation(multiprocess_server):
    url = multiprocess_server
    # 新しい接続で両ワーカーへの配送を確認する。キューなしなので、満杯なら別接続で再試行する。
    pids = set()
    for _ in range(30):
        response = httpx.post(
            url + "/invocations", json={"messages": [{"role": "user", "content": "pid"}], "stream": False}, timeout=5
        )
        assert response.status_code == 200, response.text
        pids.add(json.loads(response.json()["text"])["pid"])
        if len(pids) == 2:
            break
    assert len(pids) == 2
    job = httpx.post(
        url + "/invocations",
        json={"messages": [{"role": "user", "content": "hold"}], "mode": "job", "session_id": "shared"},
        timeout=5,
    ).json()
    assert job["state"] == "running"
    try:
        for _ in range(8):
            assert httpx.get(url + "/ping", timeout=5).status_code == 200
            duplicate = httpx.post(
                url + "/invocations",
                json={"messages": [{"role": "user", "content": "pid"}], "session_id": "shared"},
                timeout=5,
            )
            assert duplicate.status_code == 409
            status = httpx.post(url + "/invocations", json={"mode": "status", "session_id": "shared"}, timeout=5)
            assert status.json()["job_id"] == job["job_id"]
    finally:
        # 所有ワーカーに届けば即時、別ワーカーなら心拍で拾われる。終了処理でも停止を要求する。
        httpx.post(url + "/invocations", json={"mode": "cancel", "session_id": "shared"}, timeout=5)

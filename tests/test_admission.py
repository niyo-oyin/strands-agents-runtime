from __future__ import annotations

import asyncio

import pytest

from strands_runtime.definitions.schema import ExecutionSection
from strands_runtime.errors import CapacityError, SessionBusyError
from strands_runtime.invoke import InvocationRequest
from strands_runtime.invoke.admission import AdmissionController
from strands_runtime.runtime import Runtime
from strands_runtime_client.events import ResultEvent, StartEvent
from tests.conftest import write_agent, write_runtime


def limited_runtime(tmp_path, *, concurrent=1, queued=2, timeout=1):
    write_agent(tmp_path / "agents", "a")
    return Runtime.load(
        write_runtime(
            tmp_path,
            f"""default_agent = "a"
[execution]
max_concurrent = {concurrent}
max_queued = {queued}
queue_timeout_seconds = {timeout}
""",
        )
    )


async def test_concurrent_same_session_is_rejected_during_journal_read(runtime, monkeypatch):
    reading, release = asyncio.Event(), asyncio.Event()

    async def slow_check(session_id):
        reading.set()
        await release.wait()
        return False

    monkeypatch.setattr(runtime.invoker, "busy_check", slow_check)
    first = runtime.invoker.stream(InvocationRequest(prompt="go", session_id="same"))
    started = asyncio.create_task(anext(first))
    try:
        await asyncio.wait_for(reading.wait(), 1)
        with pytest.raises(SessionBusyError):
            await anext(runtime.invoker.stream(InvocationRequest(prompt="again", session_id="same")))
        release.set()
        assert isinstance(await started, StartEvent)
    finally:
        release.set()
        await started
        await first.aclose()


async def test_bounded_fifo_queue_rejects_duplicates_and_overflow():
    gate = AdmissionController(ExecutionSection(max_concurrent=1, max_queued=2, queue_timeout_seconds=1))
    order = []
    attempting = [asyncio.Event(), asyncio.Event()]

    async def enter(index):
        attempting[index].set()
        async with gate.acquire(str(index)):
            order.append(index)

    async with gate.acquire("running"):
        tasks = []
        for index in range(2):
            tasks.append(asyncio.create_task(enter(index)))
            await attempting[index].wait()
        with pytest.raises(SessionBusyError):
            async with gate.acquire("0"):
                pytest.fail("queued session was not reserved")
        with pytest.raises(CapacityError, match="full"):
            async with gate.acquire("overflow"):
                pytest.fail("queue overflow accepted")
        assert order == []
    await asyncio.wait_for(asyncio.gather(*tasks), 1)
    assert order == [0, 1]


async def test_wait_timeout_and_cancel_release_session_and_capacity():
    gate = AdmissionController(ExecutionSection(max_concurrent=1, max_queued=1, queue_timeout_seconds=0.02))
    async with gate.acquire("running"):
        with pytest.raises(CapacityError, match="timed out"):
            async with gate.acquire("next"):
                pytest.fail("unavailable slot acquired")
        attempting = asyncio.Event()

        async def wait():
            attempting.set()
            async with gate.acquire("next"):
                pytest.fail("cancelled waiter ran")

        task = asyncio.create_task(wait())
        await attempting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    async with gate.acquire("next"):
        pass


async def test_shutdown_wakes_waiters_and_rejects_new_requests():
    gate = AdmissionController(ExecutionSection(max_concurrent=1, max_queued=1, queue_timeout_seconds=60))
    attempting = asyncio.Event()

    async def wait():
        attempting.set()
        async with gate.acquire("queued"):
            pytest.fail("must not run while shutting down")

    async with gate.acquire("running"):
        task = asyncio.create_task(wait())
        await attempting.wait()
        gate.close()
        with pytest.raises(CapacityError, match="shutting down"):
            await asyncio.wait_for(task, 1)
    with pytest.raises(CapacityError, match="shutting down"):
        async with gate.acquire("new"):
            pytest.fail("shutdown accepted work")


@pytest.mark.parametrize("failure", [RuntimeError("storage failed"), asyncio.CancelledError()])
async def test_failed_busy_check_releases_session(runtime, monkeypatch, failure):
    async def fail(session_id):
        raise failure

    monkeypatch.setattr(runtime.invoker, "busy_check", fail)
    with pytest.raises(type(failure)):
        await anext(runtime.invoker.stream(InvocationRequest(prompt="go", session_id="s")))
    monkeypatch.setattr(runtime.invoker, "busy_check", None)
    stream = runtime.invoker.stream(InvocationRequest(prompt="go", session_id="s"))
    assert isinstance(await anext(stream), StartEvent)
    await stream.aclose()


async def test_job_holds_capacity_until_finished_and_observation_bypasses_it(tmp_path, monkeypatch):
    runtime = limited_runtime(tmp_path, queued=0)
    release, entered = asyncio.Event(), asyncio.Event()

    async def execute(run):
        entered.set()
        await release.wait()
        yield ResultEvent(agent="a", session_id=run.session.session_id, text="ok", stop_reason="end_turn")

    monkeypatch.setattr(runtime.invoker, "_execute", execute)
    job = await runtime.jobs.submit(InvocationRequest(prompt="go", session_id="job", mode="job"))
    await entered.wait()
    try:
        with pytest.raises(CapacityError):
            await anext(runtime.invoker.stream(InvocationRequest(prompt="go", session_id="other")))
        assert (await runtime.jobs.status("job", job.job_id)).state == "running"
        assert (await runtime.jobs.cancel("job", job.job_id)).state == "cancelling"
    finally:
        release.set()
        await runtime.jobs.shutdown()
    stream = runtime.invoker.stream(InvocationRequest(prompt="next", session_id="other"))
    assert isinstance(await anext(stream), StartEvent)
    await stream.aclose()


async def test_terminal_event_follows_cleanup_and_release_without_closing_stream(tmp_path, monkeypatch):
    runtime = limited_runtime(tmp_path, queued=0)
    closed = False

    async def execute(run):
        nonlocal closed
        try:
            yield ResultEvent(agent="a", session_id=run.session.session_id, text="ok", stop_reason="end_turn")
        finally:
            await asyncio.sleep(0)
            closed = True

    monkeypatch.setattr(runtime.invoker, "_execute", execute)
    stream = runtime.invoker.stream(InvocationRequest(prompt="go", session_id="s"))
    await anext(stream)
    assert isinstance(await anext(stream), ResultEvent)
    assert closed
    next_stream = runtime.invoker.stream(InvocationRequest(prompt="go", session_id="s"))
    assert isinstance(await anext(next_stream), StartEvent)
    await next_stream.aclose()
    await stream.aclose()

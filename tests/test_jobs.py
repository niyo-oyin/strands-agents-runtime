"""ジョブと進捗の契約（設計文書 §7.6）。JobManager を直接使い、ジャーナルはローカルの Storage に置く。"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from strands_runtime.errors import JobsDisabledError, SessionBusyError, UnknownJobError
from strands_runtime.events import ErrorEvent, JobEvent, ResultEvent, TextEvent, ToolResultEvent, ToolUseEvent
from strands_runtime.invoke import InvocationRequest
from strands_runtime.jobs import JobJournal, JobRecord
from strands_runtime.jobs import manager as manager_module
from strands_runtime.runtime import Runtime
from tests import fakes
from tests.conftest import write_agent, write_runtime


async def submit(runtime: Runtime, prompt: str = "hi", **options: object) -> JobEvent:
    return await runtime.jobs.submit(InvocationRequest(prompt=prompt, mode="job", **options))  # type: ignore[arg-type]


async def follow_all(runtime: Runtime, session_id: str, job_id: str | None = None, *, after: int = 0) -> list[object]:
    return [event async for event in runtime.jobs.follow(session_id, job_id, after=after)]


def journal_of(runtime: Runtime) -> JobJournal:
    storage = runtime.factory.sessions.storage_for(runtime.config.session)
    assert storage is not None
    return JobJournal(storage)


def stale_record(session_id: str, *, minutes_ago: int = 10) -> JobRecord:
    """別のプロセスが持っていた（そして消えた）実行中のジョブの記録。"""
    then = (datetime.now(UTC) - timedelta(minutes=minutes_ago)).replace(microsecond=0).isoformat()
    return JobRecord(
        job_id=str(uuid.uuid4()),
        session_id=session_id,
        agent="echoer",
        state="running",
        accepted_at=then,
        updated_at=then,
        seq=1,
        owner="other-host:1",
    )


async def test_job_runs_in_background_and_journals_coarse_events(runtime: Runtime) -> None:
    fakes.script(fakes.tool_use("echo", {"text": "x"}), fakes.text("done"))

    job = await submit(runtime, session_id="job-1")
    assert job.state == "running" and job.seq == 1 and job.agent == "echoer"
    assert job.last_event is not None and job.last_event.type == "start"

    events = await follow_all(runtime, "job-1", job.job_id)
    # 差分ではなくメッセージ単位の text。連番と時刻が全イベントに付く。
    assert [e.type for e in events] == ["start", "tool_use", "tool_result", "text", "result"]  # type: ignore[attr-defined]
    assert [e.seq for e in events] == [1, 2, 3, 4, 5] and all(e.ts for e in events)  # type: ignore[attr-defined]
    assert isinstance(events[3], TextEvent) and events[3].text == "done"

    status = await runtime.jobs.status("job-1")  # job_id 省略 = 最新
    assert status.state == "completed" and status.stop_reason == "end_turn"
    assert status.seq == 5 and status.finished_at is not None and status.cancelled_by is None
    assert status.last_event is not None and status.last_event.type == "result"

    result = await runtime.jobs.result("job-1", job.job_id)
    assert isinstance(result, ResultEvent) and result.text == "done" and result.seq == 5

    # カーソルからの再開と、終端後の follow。
    assert [e.type for e in await follow_all(runtime, "job-1", after=3)] == ["text", "result"]  # type: ignore[attr-defined]
    assert await follow_all(runtime, "job-1", after=5) == []

    # ジャーナルは別のプロセスからも読める（メモリ内の記録を介さない）。
    journal = journal_of(runtime)
    assert await journal.read_latest("job-1") == job.job_id
    stored = await journal.read_record("job-1", job.job_id)
    assert stored is not None and stored.state == "completed" and stored.owner
    assert [e.type for e in await journal.read_events("job-1", job.job_id, after=0, upto=5)] == [  # type: ignore[attr-defined]
        "start",
        "tool_use",
        "tool_result",
        "text",
        "result",
    ]


async def test_tool_result_detail_full_is_journaled(runtime: Runtime) -> None:
    fakes.script(fakes.tool_use("echo", {"text": "x"}), fakes.text("ok"))
    job = await submit(runtime, session_id="job-full", tool_result_detail="full")
    events = await follow_all(runtime, "job-full", job.job_id)
    tool_result = next(e for e in events if isinstance(e, ToolResultEvent))
    assert tool_result.content == [{"text": "echo:x"}] and tool_result.preview == "echo:x"


async def test_cancel_is_cooperative_and_idempotent(runtime: Runtime) -> None:
    fakes.script(fakes.text("x" * 300))
    fakes.set_chunk_delay(0.02)
    try:
        job = await submit(runtime, session_id="job-cancel")
        await asyncio.sleep(0.1)
        assert (await runtime.jobs.status("job-cancel")).state == "running"

        cancelling = await runtime.jobs.cancel("job-cancel")
        assert cancelling.state == "cancelling" and cancelling.cancelled_by == "client"

        final = (await follow_all(runtime, "job-cancel", job.job_id))[-1]
        assert isinstance(final, ResultEvent) and final.stop_reason == "cancelled"
        status = await runtime.jobs.status("job-cancel")
        assert status.state == "cancelled" and status.cancelled_by == "client" and status.finished_at
        # 終端のジョブへの cancel は記録を返すだけ。
        assert (await runtime.jobs.cancel("job-cancel")).state == "cancelled"
    finally:
        fakes.set_chunk_delay(0.0)


async def test_running_job_blocks_the_session_until_it_finishes(runtime: Runtime) -> None:
    fakes.script(fakes.text("x" * 100), fakes.text("next"))
    fakes.set_chunk_delay(0.02)
    try:
        job = await submit(runtime, session_id="job-busy")
        with pytest.raises(SessionBusyError):
            await anext(runtime.invoker.stream(InvocationRequest(prompt="again", session_id="job-busy")))
        with pytest.raises(SessionBusyError):
            await submit(runtime, session_id="job-busy")
        assert (await runtime.jobs.status("job-busy", job.job_id)).state == "running"
    finally:
        fakes.set_chunk_delay(0.0)
    await follow_all(runtime, "job-busy", job.job_id)
    result = await runtime.invoker.invoke(InvocationRequest(prompt="again", session_id="job-busy"))
    assert isinstance(result, ResultEvent) and result.text == "next"


async def test_job_running_elsewhere_blocks_the_session_too(runtime: Runtime) -> None:
    """別レプリカのジョブは、ジャーナルの記録（心拍が新しい）だけで判定する。"""
    journal = journal_of(runtime)
    record = stale_record("job-remote", minutes_ago=0)
    await journal.write_record(record)
    await journal.write_latest("job-remote", record.job_id)
    with pytest.raises(SessionBusyError):
        await anext(runtime.invoker.stream(InvocationRequest(prompt="hi", session_id="job-remote")))
    assert (await runtime.jobs.cancel("job-remote")).state == "cancelling"
    stored = await journal.read_record("job-remote", record.job_id)
    assert stored is not None and stored.cancel_requested_at and stored.cancelled_by == "client"


async def test_cancel_request_from_another_process_is_picked_up_by_the_heartbeat(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(manager_module, "HEARTBEAT_SECONDS", 0.05)
    fakes.script(fakes.text("x" * 400))
    fakes.set_chunk_delay(0.02)
    try:
        job = await submit(runtime, session_id="job-hb")
        journal = journal_of(runtime)
        stored = await journal.read_record("job-hb", job.job_id)
        assert stored is not None
        # 別プロセスの cancel と同じ内容をジャーナルに書く。
        stored.cancel_requested_at = stored.updated_at
        stored.cancelled_by = "runtime"
        stored.state = "cancelling"
        await journal.write_record(stored)
        final = (await follow_all(runtime, "job-hb", job.job_id))[-1]
    finally:
        fakes.set_chunk_delay(0.0)
    assert isinstance(final, ResultEvent) and final.stop_reason == "cancelled"
    status = await runtime.jobs.status("job-hb", job.job_id)
    assert status.state == "cancelled" and status.cancelled_by == "runtime"


async def test_lost_job_is_failed_when_observed(runtime: Runtime) -> None:
    journal = journal_of(runtime)
    record = stale_record("job-lost")
    await journal.write_record(record)
    await journal.write_latest("job-lost", record.job_id)

    status = await runtime.jobs.status("job-lost")
    assert status.state == "failed" and status.error_type == "JobLost" and status.seq == 2
    result = await runtime.jobs.result("job-lost")
    assert isinstance(result, ErrorEvent) and result.error_type == "JobLost" and result.seq == 2
    assert [e.type for e in await follow_all(runtime, "job-lost", after=1)] == ["error"]  # type: ignore[attr-defined]
    # 新しい実行は受け付けられる。
    fakes.script(fakes.text("fresh"))
    assert isinstance(await runtime.invoker.invoke(InvocationRequest(prompt="go", session_id="job-lost")), ResultEvent)


async def test_unknown_job(runtime: Runtime) -> None:
    with pytest.raises(UnknownJobError):
        await runtime.jobs.status("no-such-session")
    fakes.script(fakes.text("x"))
    job = await submit(runtime, session_id="job-known")
    await follow_all(runtime, "job-known", job.job_id)
    with pytest.raises(UnknownJobError):
        await runtime.jobs.status("job-known", "not-a-job")


async def test_failed_model_call_fails_the_job(runtime: Runtime) -> None:
    fakes.script()  # 台本が空なので偽モデルは例外を投げる
    job = await submit(runtime, session_id="job-fail")
    final = (await follow_all(runtime, "job-fail", job.job_id))[-1]
    assert isinstance(final, ErrorEvent) and final.error_type == "AssertionError"
    status = await runtime.jobs.status("job-fail")
    assert status.state == "failed" and status.error_type == "AssertionError"


async def test_jobs_need_a_storage(tmp_path: Path) -> None:
    write_agent(tmp_path / "agents", "a")
    runtime = Runtime.load(write_runtime(tmp_path, '[session]\nbackend = "none"'))
    assert not runtime.jobs.enabled
    with pytest.raises(JobsDisabledError):
        await submit(runtime, agent="a")

    # [jobs] でジャーナルの置き場を明示すれば、セッションを持たない構成でもジョブは使える。
    runtime = Runtime.load(
        write_runtime(tmp_path, '[session]\nbackend = "none"\n[jobs]\nstorage = "local"\ndir = "./jobs"')
    )
    fakes.script(fakes.text("ok"))
    job = await submit(runtime, agent="a", session_id="job-explicit")
    final = (await follow_all(runtime, "job-explicit", job.job_id))[-1]
    assert isinstance(final, ResultEvent) and final.text == "ok"
    assert (tmp_path / "jobs").is_dir()


async def test_max_duration_caps_a_job(tmp_path: Path) -> None:
    write_agent(tmp_path / "agents", "slow")
    runtime = Runtime.load(write_runtime(tmp_path, "[jobs]\nmax_duration_seconds = 0.3"))
    fakes.script(fakes.text("a" * 20), fakes.text("never"))
    fakes.set_chunk_delay(0.25)
    try:
        job = await submit(runtime, agent="slow", session_id="job-slow")
        final = (await follow_all(runtime, "job-slow", job.job_id))[-1]
    finally:
        fakes.set_chunk_delay(0.0)
    status = await runtime.jobs.status("job-slow")
    if isinstance(final, ResultEvent):
        assert final.stop_reason == "timeout" and status.state == "completed" and status.stop_reason == "timeout"
    else:
        assert isinstance(final, ErrorEvent) and final.error_type == "Timeout" and status.state == "failed"


async def test_include_subagents_tags_nested_events(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    write_agent(agents, "parent", toml_extra='subagents = ["child"]\n', with_tool=False)
    write_agent(agents, "child", with_tool=False)
    runtime = Runtime.load(write_runtime(tmp_path))
    fakes.script(fakes.tool_use("child", {"input": "hello"}), fakes.text("child says hi"), fakes.text("done"))

    request = InvocationRequest(agent="parent", prompt="go", session_id="nest-1", include_subagents=True)
    events = [e async for e in runtime.invoker.stream(request)]

    nested = [e for e in events if getattr(e, "parent", None) is not None]
    assert nested, [e.type for e in events]  # type: ignore[attr-defined]
    child_text = next(e for e in nested if isinstance(e, TextEvent))
    assert child_text.text == "child says hi" and child_text.agent == "child" and child_text.parent == "tu-1"
    # 子の結果は親の tool_result に畳まれ、result イベントは親の 1 つだけ。
    assert next(e for e in events if isinstance(e, ResultEvent)).agent == "parent"
    parent_tool_use = next(e for e in events if isinstance(e, ToolUseEvent) and e.parent is None)
    assert parent_tool_use.name == "child" and parent_tool_use.tool_use_id == "tu-1"
    # 親のテキストは差分のまま（同期実行）。
    assert "".join(e.text for e in events if isinstance(e, TextEvent) and e.parent is None) == "done"

    fakes.script(fakes.tool_use("child", {"input": "hello"}), fakes.text("child says hi"), fakes.text("done"))
    plain = [
        e async for e in runtime.invoker.stream(InvocationRequest(agent="parent", prompt="go", session_id="nest-2"))
    ]
    assert all(getattr(e, "parent", None) is None for e in plain)


async def test_job_journal_on_s3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_CONFIG_FILE": "/dev/null",
        "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with mock_aws():
        boto3.client("s3").create_bucket(Bucket="job-bucket")
        prefix = f"p/{uuid.uuid4()}/"
        write_agent(tmp_path / "agents", "s3agent")
        runtime = Runtime.load(
            write_runtime(tmp_path, f'[session]\nbackend = "s3"\nbucket = "job-bucket"\nprefix = "{prefix}"')
        )
        fakes.script(fakes.text("on s3"))
        job = await submit(runtime, agent="s3agent", session_id="job-s3")
        final = (await follow_all(runtime, "job-s3", job.job_id))[-1]
        assert isinstance(final, ResultEvent) and final.text == "on s3"
        keys = [
            o["Key"] for o in boto3.client("s3").list_objects_v2(Bucket="job-bucket", Prefix=prefix).get("Contents", [])
        ]
        assert any(f"jobs/job-s3/{job.job_id}/job.json" in k for k in keys), keys
        assert any(f"jobs/job-s3/{job.job_id}/events/000002" in k for k in keys), keys

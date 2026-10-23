"""ジョブの受付・実行・観測（設計文書 §7.6）。

ジョブは :class:`~strands_runtime.invoke.Invoker` の同じストリームをバックグラウンドタスクで消費し、
粗い粒度のイベントをジャーナルに書く。``status`` / ``follow`` / ``result`` はジャーナルを正とし、
このプロセスがジョブを持っているときだけメモリ内の記録で即時に応える。
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

from opentelemetry import trace

from strands_runtime.errors import JobsDisabledError, UnknownJobError
from strands_runtime.events import (
    CancelledBy,
    ErrorEvent,
    InterruptEvent,
    JobEvent,
    JobLastEvent,
    JobState,
    ResultEvent,
    StartEvent,
    TextEvent,
    ToolResultEvent,
    ToolUseEvent,
)
from strands_runtime.invoke.invoker import CANCEL_DRAIN_SECONDS, InvocationRequest, Invoker
from strands_runtime.jobs.journal import JobJournal, JobRecord, seconds_since, utcnow

logger = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 30.0
"""実行中のジョブが ``job.json`` の ``updated_at`` を進める間隔。"""
LOST_AFTER_SECONDS = HEARTBEAT_SECONDS * 3
"""この時間、心拍が止まった実行中のジョブはプロセスごと消えたとみなす。"""
FOLLOW_POLL_SECONDS = 1.0
"""ジョブを持たないプロセスで ``follow`` するときの、ジャーナルを読み直す間隔。"""
SUMMARY_CHARS = 80
JOB_ID_ATTRIBUTE = "strands_runtime.job.id"
"""受付・観測のリクエストのスパンと、実行側の ``invoke_agent`` スパンの両方に付け、可視化ツールで突き合わせる。"""


class BusyTracker(Protocol):
    """実行中のジョブの有無を ``/ping`` に反映させるための差し込み口（AgentCore では ``HealthyBusy``）。"""

    def start(self, name: str) -> Any: ...
    def finish(self, handle: Any) -> object: ...


@dataclass
class _RunningJob:
    """このプロセスが実行しているジョブ。``events`` はジャーナルと同じ内容のメモリ内コピー。"""

    record: JobRecord
    cancel: threading.Event
    events: list[Any] = field(default_factory=list)
    changed: asyncio.Condition = field(default_factory=asyncio.Condition)
    finished: bool = False
    task: asyncio.Task[None] | None = None
    heartbeat: asyncio.Task[None] | None = None
    busy_handle: Any = None


class JobManager:
    def __init__(
        self,
        invoker: Invoker,
        journal: JobJournal | None,
        *,
        max_duration_seconds: float,
        busy_tracker: BusyTracker | None = None,
    ) -> None:
        self._invoker = invoker
        self._journal = journal
        self._max_duration = max_duration_seconds
        self.busy_tracker = busy_tracker
        self._running: dict[str, _RunningJob] = {}
        self._owner = f"{os.uname().nodename}:{os.getpid()}"

    @property
    def enabled(self) -> bool:
        return self._journal is not None

    # ---- 受付 --------------------------------------------------------------------------------------------------

    async def submit(self, request: InvocationRequest, *, session_id: str | None = None) -> JobEvent:
        """ジョブを受け付け、実行をバックグラウンドで始めて、受付直後の記録を返す。

        :class:`UnknownAgentError` / :class:`SessionBusyError` は Invoker が最初のイベントを返す前に送出するので、
        ここでもそのまま呼び出し側（サーバー層）に伝わる。
        """
        journal = self._require_journal()
        job_id = str(uuid.uuid4())
        _tag_span(job_id)
        cancel = threading.Event()
        # ジョブでは差分を流さず、メッセージ単位のテキストをジャーナルに書く。推論は記録しない。
        effective = request.model_copy(update={"include_reasoning": False, "stream": True})
        stream = self._invoker.stream(
            effective,
            session_id=session_id,
            cancel_signal=cancel,
            coalesce_text=True,
            timeout_cap=self._max_duration,
            span_attributes={JOB_ID_ATTRIBUTE: job_id},
        )
        first = await anext(stream)
        assert isinstance(first, StartEvent)

        now = utcnow()
        record = JobRecord(
            job_id=job_id,
            session_id=first.session_id,
            agent=first.agent,
            state="running",
            accepted_at=now,
            updated_at=now,
            owner=self._owner,
        )
        job = _RunningJob(record=record, cancel=cancel)
        self._running[record.job_id] = job
        if self.busy_tracker is not None:
            job.busy_handle = self.busy_tracker.start(f"job:{record.agent}")
        await journal.write_record(record)
        await journal.write_latest(record.session_id, record.job_id)
        await self._append(job, first)

        job.task = asyncio.create_task(self._consume(job, stream), name=f"job-{record.job_id}")
        job.heartbeat = asyncio.create_task(self._heartbeat(job), name=f"job-heartbeat-{record.job_id}")
        return record.as_event()

    async def _consume(self, job: _RunningJob, stream: AsyncIterator[Any]) -> None:
        """Invoker のストリームを最後まで読み、ジャーナルに書く。終端で状態を確定する。"""
        try:
            async for event in stream:
                await self._append(job, event)
        except Exception:
            # Invoker は例外を ``error`` イベントにして返すので、ここに来るのはジャーナル側の想定外の失敗だけ。
            logger.exception("job %s failed outside the agent loop", job.record.job_id)
            await self._append(job, ErrorEvent(error_type="JobFailed", message="the job loop raised unexpectedly"))
        finally:
            await self._finish(job)

    async def _finish(self, job: _RunningJob) -> None:
        if job.heartbeat is not None:
            job.heartbeat.cancel()
        if job.busy_handle is not None and self.busy_tracker is not None:
            self.busy_tracker.finish(job.busy_handle)
        self._running.pop(job.record.job_id, None)
        async with job.changed:
            job.finished = True
            job.changed.notify_all()

    async def _append(self, job: _RunningJob, event: Any) -> None:
        """イベントに連番と時刻を付け、メモリとジャーナルの両方に記録し、必要なら状態を確定する。"""
        journal = self._require_journal()
        record = job.record
        seq = record.seq + 1
        stamped = event.model_copy(update={"seq": seq, "ts": utcnow()})
        await journal.write_event(record.session_id, record.job_id, stamped)

        record.seq = seq
        record.updated_at = stamped.ts
        record.last_event = JobLastEvent(seq=seq, ts=stamped.ts, type=stamped.type, summary=_summary(stamped))
        if isinstance(stamped, ResultEvent):
            record.stop_reason = stamped.stop_reason
            record.state = _state_for_result(stamped.stop_reason)
            record.finished_at = stamped.ts
        elif isinstance(stamped, ErrorEvent):
            record.error_type = stamped.error_type
            record.state = "failed"
            record.finished_at = stamped.ts
        if record.finished and record.state != "cancelled":
            # 停止要求の後に通常どおり終わった場合、「誰が止めたか」は意味を持たない。
            record.cancelled_by = None
        await journal.write_record(record)

        async with job.changed:
            job.events.append(stamped)
            job.changed.notify_all()

    async def _heartbeat(self, job: _RunningJob) -> None:
        """定期的に ``updated_at`` を進め、別プロセスからの停止要求がジャーナルに書かれていないか確かめる。"""
        journal = self._require_journal()
        record = job.record
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            if job.finished:
                return
            if not job.cancel.is_set():
                stored = await journal.read_record(record.session_id, record.job_id)
                if stored is not None and stored.cancel_requested_at is not None:
                    self._request_cancel(job, stored.cancelled_by or "client", stored.cancel_requested_at)
            record.updated_at = utcnow()
            await journal.write_record(record)

    # ---- 観測 --------------------------------------------------------------------------------------------------

    async def status(self, session_id: str, job_id: str | None = None) -> JobEvent:
        return (await self._load(session_id, job_id)).as_event()

    async def result(self, session_id: str, job_id: str | None = None) -> ResultEvent | ErrorEvent | JobEvent:
        """終端なら最後に記録した ``result`` / ``error`` を、実行中なら現在の記録を返す。"""
        record = await self._load(session_id, job_id)
        if not record.finished:
            return record.as_event()
        journal = self._require_journal()
        events = await journal.read_events(record.session_id, record.job_id, after=record.seq - 1, upto=record.seq)
        if events and isinstance(events[0], ResultEvent | ErrorEvent):
            return events[0]
        # 終端イベントの書き込みだけが失われた場合。記録の状態から同じ意味のエラーを返す。
        return ErrorEvent(error_type=record.error_type or "JournalIncomplete", message="terminal event is missing")

    async def follow(self, session_id: str, job_id: str | None = None, *, after: int = 0) -> AsyncIterator[Any]:
        """``after`` の次からイベントを再生し、実行中ならそのまま終端まで追従する。"""
        record = await self._load(session_id, job_id)
        job = self._running.get(record.job_id)
        if job is not None:
            async for event in self._follow_local(job, after):
                yield event
            return
        async for event in self._follow_journal(record, after):
            yield event

    async def _follow_local(self, job: _RunningJob, after: int) -> AsyncIterator[Any]:
        """このプロセスが持つジョブ。メモリ内の記録を読み、新しいイベントは通知で待つ。"""
        index = after
        while True:
            async with job.changed:
                while len(job.events) <= index and not job.finished:
                    await job.changed.wait()
                pending = job.events[index:]
                finished = job.finished
            for event in pending:
                yield event
            index += len(pending)
            if finished and len(job.events) <= index:
                return

    async def _follow_journal(self, record: JobRecord, after: int) -> AsyncIterator[Any]:
        """別のプロセスが持つ（または終わった）ジョブ。ジャーナルを一定間隔で読み直す。"""
        journal = self._require_journal()
        cursor = after
        while True:
            for event in await journal.read_events(record.session_id, record.job_id, after=cursor, upto=record.seq):
                yield event
            cursor = max(cursor, record.seq)
            if record.finished:
                return
            await asyncio.sleep(FOLLOW_POLL_SECONDS)
            record = await self._load(record.session_id, record.job_id)

    # ---- 停止 --------------------------------------------------------------------------------------------------

    async def cancel(self, session_id: str, job_id: str | None = None, *, by: CancelledBy = "client") -> JobEvent:
        """停止を要求する。冪等で、終端のジョブには現在の記録を返すだけ。"""
        record = await self._load(session_id, job_id)
        if record.finished:
            return record.as_event()
        job = self._running.get(record.job_id)
        now = utcnow()
        if job is not None:
            self._request_cancel(job, by, now)
            record = job.record
        else:
            # 別プロセスのジョブ。要求をジャーナルに書き、そのプロセスの心拍に拾わせる。
            record.cancel_requested_at = now
            record.cancelled_by = by
            record.state = "cancelling"
        await self._require_journal().write_record(record)
        return record.as_event()

    def _request_cancel(self, job: _RunningJob, by: CancelledBy, at: str) -> None:
        record = job.record
        record.cancel_requested_at = at
        record.cancelled_by = by
        record.state = "cancelling"
        job.cancel.set()

    async def shutdown(self) -> None:
        """実行中の全ジョブに停止を要求し、猶予まで待つ（グレースフルシャットダウン）。"""
        jobs = list(self._running.values())
        for job in jobs:
            await self.cancel(job.record.session_id, job.record.job_id, by="runtime")
        tasks = [job.task for job in jobs if job.task is not None]
        if tasks:
            await asyncio.wait(tasks, timeout=CANCEL_DRAIN_SECONDS + 5)

    # ---- 排他 --------------------------------------------------------------------------------------------------

    async def session_busy(self, session_id: str) -> bool:
        """最新のジョブが実行中で、心拍が新しいか自プロセスのものなら、そのセッションは使用中。

        Invoker は同期実行とジョブのどちらを始める前にもこれを呼ぶので、別レプリカのジョブとの競合も 409 になる。
        """
        if self._journal is None:
            return False
        latest = await self._journal.read_latest(session_id)
        if latest is None:
            return False
        record = await self._journal.read_record(session_id, latest)
        if record is None or record.finished:
            return False
        return latest in self._running or seconds_since(record.updated_at) < LOST_AFTER_SECONDS

    # ---- 内部 --------------------------------------------------------------------------------------------------

    def _require_journal(self) -> JobJournal:
        if self._journal is None:
            raise JobsDisabledError()
        return self._journal

    async def _load(self, session_id: str, job_id: str | None) -> JobRecord:
        """記録を読む。心拍が途絶えた実行中のジョブは、ここで ``JobLost`` の失敗として確定する。"""
        journal = self._require_journal()
        resolved = job_id or await journal.read_latest(session_id)
        if resolved is None:
            raise UnknownJobError(session_id, job_id)
        _tag_span(resolved)
        job = self._running.get(resolved)
        if job is not None:
            return job.record
        record = await journal.read_record(session_id, resolved)
        if record is None:
            raise UnknownJobError(session_id, job_id)
        if not record.finished and seconds_since(record.updated_at) >= LOST_AFTER_SECONDS:
            record = await self._mark_lost(record)
        return record

    async def _mark_lost(self, record: JobRecord) -> JobRecord:
        journal = self._require_journal()
        now = utcnow()
        lost = ErrorEvent(
            error_type="JobLost",
            message=f"no heartbeat since {record.updated_at}; the process running this job is gone",
            seq=record.seq + 1,
            ts=now,
        )
        await journal.write_event(record.session_id, record.job_id, lost)
        record.seq = lost.seq or record.seq + 1
        record.updated_at = now
        record.finished_at = now
        record.state = "failed"
        record.error_type = "JobLost"
        record.cancelled_by = None
        record.last_event = JobLastEvent(seq=record.seq, ts=now, type="error", summary="JobLost")
        await journal.write_record(record)
        return record


def _tag_span(job_id: str) -> None:
    span = trace.get_current_span()
    if span.is_recording():
        span.set_attribute(JOB_ID_ATTRIBUTE, job_id)


def _state_for_result(stop_reason: str) -> JobState:
    if stop_reason == "cancelled":
        return "cancelled"
    if stop_reason == "interrupt":
        return "input_required"
    return "completed"


def _summary(event: Any) -> str:
    match event:
        case StartEvent():
            return event.agent
        case TextEvent():
            return _head(event.text)
        case ToolUseEvent():
            return event.name
        case ToolResultEvent():
            return f"{event.status}: {_head(event.preview)}" if event.preview else event.status
        case InterruptEvent():
            return ", ".join(i.name for i in event.interrupts)
        case ResultEvent():
            return event.stop_reason
        case ErrorEvent():
            return event.error_type
        case _:
            return ""


def _head(text: str) -> str:
    return text if len(text) <= SUMMARY_CHARS else text[:SUMMARY_CHARS] + "…"

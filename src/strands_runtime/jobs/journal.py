"""ジョブのジャーナル。進捗イベントと記録（状態、心拍、カーソル）を設定された保存先に書く。

キー配置（設計文書 §7.6.4）::

    jobs/{session_id}/latest                   最新の job_id
    jobs/{session_id}/{job_id}/job.json        JobRecord
    jobs/{session_id}/{job_id}/events/{seq}    イベント 1 件ずつ（seq は 6 桁ゼロ埋めの連番）

ストレージはプロセスの外にあるので、``status`` / ``result`` / ``follow`` はジョブを実行していないプロセスからでも答えられる。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel

from strands_runtime_client.events import CancelledBy, JobEvent, dump_event, parse_event

logger = logging.getLogger(__name__)


def utcnow() -> str:
    """ジャーナルの時刻表記（秒精度の ISO 8601、UTC、``Z`` 終わり）。"""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def seconds_since(timestamp: str) -> float:
    return (datetime.now(UTC) - datetime.fromisoformat(timestamp)).total_seconds()


class JobRecord(JobEvent):
    """``job.json`` の内容。クライアントに見せる :class:`JobEvent` に、プロセス間の調整に使う項目を足したもの。"""

    cancel_requested_at: str | None = None
    """停止要求を受けた時刻。別のプロセスで動いているジョブは心拍のたびにこれを見て自分で停止する。"""
    owner: str | None = None
    """ジョブを実行しているプロセスの識別子（診断用）。"""

    def as_event(self) -> JobEvent:
        return JobEvent.model_validate(self.model_dump(exclude={"cancel_requested_at", "owner"}))


class JobCancellation(BaseModel):
    requested_at: str
    by: CancelledBy


class JournalStorage(Protocol):
    async def read(self, key: str) -> bytes | None: ...
    async def write(self, key: str, data: bytes) -> None: ...


class JobJournal:
    def __init__(self, storage: JournalStorage) -> None:
        self._storage = storage

    @staticmethod
    def _record_key(session_id: str, job_id: str) -> str:
        return f"jobs/{session_id}/{job_id}/job.json"

    @staticmethod
    def _event_key(session_id: str, job_id: str, seq: int) -> str:
        return f"jobs/{session_id}/{job_id}/events/{seq:06d}"

    @staticmethod
    def _latest_key(session_id: str) -> str:
        return f"jobs/{session_id}/latest"

    async def write_record(self, record: JobRecord) -> None:
        await self._write(self._record_key(record.session_id, record.job_id), record.model_dump_json().encode("utf-8"))

    async def read_record(self, session_id: str, job_id: str) -> JobRecord | None:
        data = await self._storage.read(self._record_key(session_id, job_id))
        return JobRecord.model_validate_json(data) if data else None

    async def write_latest(self, session_id: str, job_id: str) -> None:
        await self._write(self._latest_key(session_id), job_id.encode("utf-8"))

    async def read_latest(self, session_id: str) -> str | None:
        data = await self._storage.read(self._latest_key(session_id))
        return data.decode("utf-8") if data else None

    async def request_cancel(self, session_id: str, job_id: str, cancellation: JobCancellation) -> None:
        # 所有ワーカーが更新する job.json と分け、心拍更新による停止要求の取りこぼしを防ぐ。
        await self._storage.write(
            f"jobs/{session_id}/{job_id}/cancel.json", cancellation.model_dump_json().encode("utf-8")
        )

    async def read_cancellation(self, session_id: str, job_id: str) -> JobCancellation | None:
        data = await self._storage.read(f"jobs/{session_id}/{job_id}/cancel.json")
        return JobCancellation.model_validate_json(data) if data else None

    async def write_event(self, session_id: str, job_id: str, event: Any) -> None:
        """``seq`` の付いたイベントを 1 件書く。"""
        assert event.seq is not None
        payload = dump_event(event)
        await self._write(self._event_key(session_id, job_id, event.seq), _json_bytes(payload))

    async def read_events(self, session_id: str, job_id: str, *, after: int, upto: int) -> list[Any]:
        """``after`` より後、``upto`` までの連番のイベントを順に返す。書き込みに失敗していた番号は飛ばす。"""
        events: list[Any] = []
        for seq in range(after + 1, upto + 1):
            data = await self._storage.read(self._event_key(session_id, job_id, seq))
            if data:
                events.append(parse_event(data))
        return events

    async def _write(self, key: str, data: bytes) -> None:
        # ジャーナルの書き込み失敗でジョブ自体を止めない。次の書き込みで記録は追いつく。
        try:
            await self._storage.write(key, data)
        except ValueError:
            # サイズ超過など再試行しても保存できない記録は、成功した扱いにしない。
            raise
        except Exception:
            logger.warning("job journal write failed (key=%s)", key, exc_info=True)


def _json_bytes(data: dict[str, Any]) -> bytes:
    return json.dumps(data, ensure_ascii=False).encode("utf-8")

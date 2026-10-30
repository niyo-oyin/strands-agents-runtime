"""クライアント共通のインターフェースと、SSE の復号。"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from typing import Any

from strands_runtime_client.events import ErrorEvent, JobEvent, ResultEvent, parse_event


class InvocationError(Exception):
    """ランタイムがエラーを返した（``error`` イベント、または 2xx 以外の応答）。"""

    def __init__(self, error_type: str, message: str) -> None:
        self.error_type = error_type
        super().__init__(f"{error_type}: {message}")


def new_session_id() -> str:
    """新しいセッション ID。UUID4 の文字列は 36 文字で、AgentCore が求める 33 文字以上を満たす。"""
    return str(uuid.uuid4())


def iter_sse_events(lines: Iterable[str | bytes]) -> Iterator[Any]:
    """SSE の行から ``data: {...}`` フレームだけを取り出してイベントにする。他のフィールドや空行は読み飛ばす。"""
    for raw in lines:
        line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        line = line.rstrip("\r\n")
        if line.startswith("data:"):
            body = line[5:].strip()
            if body:
                yield parse_event(body)


class AgentClient(ABC):
    """同期クライアント。トランスポートが違っても同じ形で使える。"""

    @abstractmethod
    def _request(self, payload: dict[str, Any], *, session_id: str | None) -> Iterator[Any]:
        """``POST /invocations`` を 1 回送り、応答をイベントとして返す（SSE なら複数、JSON なら 1 つ）。

        2xx 以外の応答は :class:`InvocationError` にする。
        """

    def stream(
        self,
        prompt: str | list[dict[str, Any]],
        *,
        agent: str | None = None,
        session_id: str | None = None,
        actor_id: str | None = None,
        invocation_state: dict[str, Any] | None = None,
    ) -> Iterator[Any]:
        """ランタイムのイベントを届いた順に返す。"""
        payload = self._payload(prompt, agent, session_id, actor_id, invocation_state)
        return self._request(payload, session_id=session_id)

    def submit(
        self,
        prompt: str | list[dict[str, Any]],
        *,
        agent: str | None = None,
        session_id: str | None = None,
        actor_id: str | None = None,
        invocation_state: dict[str, Any] | None = None,
        tool_result_detail: str = "preview",
        include_subagents: bool = False,
    ) -> JobHandle:
        """ジョブとして投入し（``mode = "job"``）、すぐに :class:`JobHandle` を返す。

        ``session_id`` を省略するとここで生成する。後続の操作は同じセッションに送る必要があるため、
        ハンドルがセッション ID を保持する。
        """
        sid = session_id or new_session_id()
        payload = self._payload(prompt, agent, sid, actor_id, invocation_state)
        payload.update(
            {"mode": "job", "tool_result_detail": tool_result_detail, "include_subagents": include_subagents}
        )
        event = self._single(payload, session_id=sid)
        if not isinstance(event, JobEvent):
            raise InvocationError("UnexpectedResponse", f"expected a job event, got {event.type}")
        return JobHandle(self, event.session_id, event.job_id)

    def job(self, session_id: str, job_id: str | None = None) -> JobHandle:
        """既存のジョブのハンドル。``job_id`` を省略するとそのセッションの最新のジョブを指す。"""
        if job_id is None:
            job_id = self._job_event({"mode": "status"}, session_id).job_id
        return JobHandle(self, session_id, job_id)

    def _single(self, payload: dict[str, Any], *, session_id: str) -> Any:
        """JSON で 1 イベント返る操作。"""
        for event in self._request(payload, session_id=session_id):
            return event
        raise InvocationError("NoResponse", "the runtime returned no event")

    def _job_event(self, payload: dict[str, Any], session_id: str) -> JobEvent:
        event = self._single({**payload, "session_id": session_id}, session_id=session_id)
        if not isinstance(event, JobEvent):
            raise InvocationError("UnexpectedResponse", f"expected a job event, got {event.type}")
        return event

    def invoke(
        self,
        prompt: str | list[dict[str, Any]],
        *,
        agent: str | None = None,
        session_id: str | None = None,
        actor_id: str | None = None,
        invocation_state: dict[str, Any] | None = None,
    ) -> ResultEvent:
        """最後まで実行して ``result`` を返す。ランタイムが ``error`` を返したら :class:`InvocationError`。"""
        for event in self.stream(
            prompt, agent=agent, session_id=session_id, actor_id=actor_id, invocation_state=invocation_state
        ):
            if isinstance(event, ResultEvent):
                return event
            if isinstance(event, ErrorEvent):
                raise InvocationError(event.error_type, event.message)
        raise InvocationError("NoResult", "the stream ended without a result event")

    @staticmethod
    def _payload(
        prompt: str | list[dict[str, Any]],
        agent: str | None,
        session_id: str | None,
        actor_id: str | None,
        invocation_state: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """``POST /invocations`` のペイロード。未指定の項目は送らず、サーバー側の既定に任せる。"""
        payload: dict[str, Any] = {"prompt": prompt, "stream": True}
        if agent is not None:
            payload["agent"] = agent
        if session_id is not None:
            payload["session_id"] = session_id
        if actor_id is not None:
            payload["actor_id"] = actor_id
        if invocation_state:
            payload["invocation_state"] = invocation_state
        return payload


class JobHandle:
    """投入済みのジョブ 1 つに対する操作（設計文書 §7.6.2 の ``status`` / ``follow`` / ``result`` / ``cancel``）。"""

    def __init__(self, client: AgentClient, session_id: str, job_id: str) -> None:
        self._client = client
        self.session_id = session_id
        self.job_id = job_id

    def _payload(self, mode: str, **extra: Any) -> dict[str, Any]:
        return {"mode": mode, "session_id": self.session_id, "job_id": self.job_id, **extra}

    def status(self) -> JobEvent:
        return self._client._job_event(self._payload("status"), self.session_id)

    def follow(self, after: int = 0) -> Iterator[Any]:
        """``after`` の次のイベントから再生し、実行中なら終端まで追従する。切断したら最後の ``seq`` で呼び直す。"""
        return self._client._request(self._payload("follow", after=after), session_id=self.session_id)

    def result(self, *, wait: bool = False) -> ResultEvent | None:
        """完了していれば ``result`` を返す。失敗していれば :class:`InvocationError`。

        未完了のときは ``wait=False`` なら ``None``、``wait=True`` なら終端まで追従してから返す。
        """
        event = self._client._single(self._payload("result"), session_id=self.session_id)
        if isinstance(event, JobEvent) and wait:
            final: Any = None
            for item in self.follow(after=max(event.seq - 1, 0)):
                final = item
            event = final
        return _unwrap_result(event)

    def cancel(self) -> JobEvent:
        """停止を要求する。冪等で、終端のジョブには現在の記録が返るだけ。"""
        return self._client._job_event(self._payload("cancel"), self.session_id)


def _unwrap_result(event: Any) -> ResultEvent | None:
    if isinstance(event, ResultEvent):
        return event
    if isinstance(event, ErrorEvent):
        raise InvocationError(event.error_type, event.message)
    return None

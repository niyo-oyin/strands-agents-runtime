"""クライアント共通のインターフェースと、SSE の復号。"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from typing import Any

from strands_runtime.events import ErrorEvent, ResultEvent, parse_event


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

"""クライアント共通のインターフェースと、SSE の復号。"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator, Sequence
from typing import Any

from pydantic import BaseModel

from strands_runtime_client.events import ErrorEvent, JobEvent, ResultEvent, parse_event
from strands_runtime_client.inputs import InvocationInput, Message, ReasoningEffort, ToolChoice
from strands_runtime_client.structured import (
    ResponseFormatInput,
    parse_events,
    parse_result,
    response_model,
    wire_response_format,
)


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
        *,
        messages: Sequence[Message | dict[str, Any]],
        store: bool | None = None,
        model: str | None = None,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        reasoning_effort: ReasoningEffort | None = None,
        response_format: ResponseFormatInput = None,
        tool_choice: ToolChoice | dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
        max_turns: int | None = None,
        max_total_tokens: int | None = None,
        agent: str | None = None,
        session_id: str | None = None,
        actor_id: str | None = None,
        invocation_state: dict[str, Any] | None = None,
    ) -> Iterator[Any]:
        """ランタイムのイベントを届いた順に返す。"""
        payload = self._payload(
            agent,
            session_id,
            actor_id,
            invocation_state,
            messages=messages,
            store=store,
            model=model,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
            top_p=top_p,
            reasoning_effort=reasoning_effort,
            response_format=response_format,
            tool_choice=tool_choice,
            timeout_seconds=timeout_seconds,
            max_turns=max_turns,
            max_total_tokens=max_total_tokens,
        )
        return parse_events(self._request(payload, session_id=session_id), response_model(response_format))

    def submit(
        self,
        *,
        messages: Sequence[Message | dict[str, Any]],
        store: bool | None = None,
        model: str | None = None,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        reasoning_effort: ReasoningEffort | None = None,
        response_format: ResponseFormatInput = None,
        tool_choice: ToolChoice | dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
        max_turns: int | None = None,
        max_total_tokens: int | None = None,
        agent: str | None = None,
        session_id: str | None = None,
        actor_id: str | None = None,
        invocation_state: dict[str, Any] | None = None,
        tool_result_detail: str = "preview",
        include_subagents: bool = False,
    ) -> JobHandle:
        """ジョブとして投入し（``mode = "job"``）、すぐに :class:`JobHandle` を返す。

        ``session_id`` を省略するとサーバーまたはトランスポートが生成する。
        後続の操作に使うセッション ID は、受付結果からハンドルに保持する。
        """
        payload = self._payload(
            agent,
            session_id,
            actor_id,
            invocation_state,
            messages=messages,
            store=store,
            model=model,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
            top_p=top_p,
            reasoning_effort=reasoning_effort,
            response_format=response_format,
            tool_choice=tool_choice,
            timeout_seconds=timeout_seconds,
            max_turns=max_turns,
            max_total_tokens=max_total_tokens,
        )
        payload.update(
            {"mode": "job", "tool_result_detail": tool_result_detail, "include_subagents": include_subagents}
        )
        event = self._single(payload, session_id=session_id)
        if not isinstance(event, JobEvent):
            raise InvocationError("UnexpectedResponse", f"expected a job event, got {event.type}")
        return JobHandle(
            self, event.session_id, event.job_id, actor_id=event.actor_id, output_model=response_model(response_format)
        )

    def job(
        self,
        session_id: str,
        job_id: str | None = None,
        *,
        actor_id: str | None = None,
        response_format: type[BaseModel] | None = None,
    ) -> JobHandle:
        """既存のジョブのハンドル。``job_id`` を省略するとそのセッションの最新のジョブを指す。"""
        output_model = response_model(response_format)
        if response_format is not None and output_model is None:
            raise TypeError("job response_format must be a Pydantic BaseModel subclass")
        if job_id is None:
            event = self._job_event(
                {"mode": "status", **({"actor_id": actor_id} if actor_id is not None else {})}, session_id
            )
            job_id, actor_id = event.job_id, event.actor_id
        return JobHandle(self, session_id, job_id, actor_id=actor_id, output_model=output_model)

    def _single(self, payload: dict[str, Any], *, session_id: str | None) -> Any:
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
        *,
        messages: Sequence[Message | dict[str, Any]],
        store: bool | None = None,
        model: str | None = None,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        reasoning_effort: ReasoningEffort | None = None,
        response_format: ResponseFormatInput = None,
        tool_choice: ToolChoice | dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
        max_turns: int | None = None,
        max_total_tokens: int | None = None,
        agent: str | None = None,
        session_id: str | None = None,
        actor_id: str | None = None,
        invocation_state: dict[str, Any] | None = None,
    ) -> ResultEvent:
        """最後まで実行して ``result`` を返す。ランタイムが ``error`` を返したら :class:`InvocationError`。"""
        for event in self.stream(
            messages=messages,
            store=store,
            model=model,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
            top_p=top_p,
            reasoning_effort=reasoning_effort,
            response_format=response_format,
            tool_choice=tool_choice,
            timeout_seconds=timeout_seconds,
            max_turns=max_turns,
            max_total_tokens=max_total_tokens,
            agent=agent,
            session_id=session_id,
            actor_id=actor_id,
            invocation_state=invocation_state,
        ):
            if isinstance(event, ResultEvent):
                return event
            if isinstance(event, ErrorEvent):
                raise InvocationError(event.error_type, event.message)
        raise InvocationError("NoResult", "the stream ended without a result event")

    @staticmethod
    def _payload(
        agent: str | None,
        session_id: str | None,
        actor_id: str | None,
        invocation_state: dict[str, Any] | None,
        *,
        messages: Sequence[Message | dict[str, Any]],
        store: bool | None = None,
        model: str | None = None,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        reasoning_effort: ReasoningEffort | None = None,
        response_format: ResponseFormatInput = None,
        tool_choice: ToolChoice | dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
        max_turns: int | None = None,
        max_total_tokens: int | None = None,
    ) -> dict[str, Any]:
        """``POST /invocations`` のペイロード。未指定の項目は送らず、サーバー側の既定に任せる。"""
        inputs = InvocationInput.model_validate(
            {
                "messages": messages,
                "store": store,
                "actor_id": actor_id,
                "session_id": session_id,
                "model": model,
                "max_output_tokens": max_output_tokens,
                "temperature": temperature,
                "top_p": top_p,
                "reasoning_effort": reasoning_effort,
                "response_format": wire_response_format(response_format),
                "tool_choice": tool_choice,
                "timeout_seconds": timeout_seconds,
                "max_turns": max_turns,
                "max_total_tokens": max_total_tokens,
            }
        )
        inputs.require_input()
        payload: dict[str, Any] = {**inputs.model_dump(exclude_none=True, by_alias=True), "stream": True}
        if agent is not None:
            payload["agent"] = agent
        if invocation_state:
            payload["invocation_state"] = invocation_state
        return payload


class JobHandle:
    """投入済みのジョブ 1 つに対する操作（設計文書 §7.6.2 の ``status`` / ``follow`` / ``result`` / ``cancel``）。"""

    def __init__(
        self,
        client: AgentClient,
        session_id: str,
        job_id: str,
        *,
        actor_id: str | None = None,
        output_model: type[BaseModel] | None = None,
    ) -> None:
        self._client = client
        self._output_model = output_model
        self.session_id = session_id
        self.job_id = job_id
        self.actor_id = actor_id

    def _payload(self, mode: str, **extra: Any) -> dict[str, Any]:
        return {
            "mode": mode,
            "session_id": self.session_id,
            "job_id": self.job_id,
            **({"actor_id": self.actor_id} if self.actor_id is not None else {}),
            **extra,
        }

    def status(self) -> JobEvent:
        return self._client._job_event(self._payload("status"), self.session_id)

    def follow(self, after: int = 0) -> Iterator[Any]:
        """``after`` の次のイベントから再生し、実行中なら終端まで追従する。切断したら最後の ``seq`` で呼び直す。"""
        return parse_events(
            self._client._request(self._payload("follow", after=after), session_id=self.session_id), self._output_model
        )

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
        result = _unwrap_result(event)
        return parse_result(result, self._output_model) if result is not None else None

    def cancel(self) -> JobEvent:
        """停止を要求する。冪等で、終端のジョブには現在の記録が返るだけ。"""
        return self._client._job_event(self._payload("cancel"), self.session_id)


def _unwrap_result(event: Any) -> ResultEvent | None:
    if isinstance(event, ResultEvent):
        return event
    if isinstance(event, ErrorEvent):
        raise InvocationError(event.error_type, event.message)
    return None

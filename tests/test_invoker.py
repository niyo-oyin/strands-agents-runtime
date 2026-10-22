from __future__ import annotations

import pytest

from strands_runtime.errors import UnknownAgentError
from strands_runtime.events import (
    ErrorEvent,
    ResultEvent,
    StartEvent,
    TextEvent,
    ToolResultEvent,
    ToolUseEvent,
)
from strands_runtime.invoke import InvocationRequest
from strands_runtime.invoke.invoker import normalize
from strands_runtime.runtime import Runtime
from tests import fakes


async def collect(runtime: Runtime, request: InvocationRequest, **kwargs: object) -> list[object]:
    return [event async for event in runtime.invoker.stream(request, **kwargs)]  # type: ignore[arg-type]


def test_normalize_text_and_messages() -> None:
    assert normalize({"data": "hi", "delta": {}}, agent="a", session_id="s") == [TextEvent(text="hi")]
    assert normalize({"data": "", "delta": {}}, agent="a", session_id="s") == []
    tool_use_msg = {
        "role": "assistant",
        "content": [{"toolUse": {"toolUseId": "t1", "name": "echo", "input": {"x": 1}}}],
    }
    assert normalize({"message": tool_use_msg}, agent="a", session_id="s") == [
        ToolUseEvent(tool_use_id="t1", name="echo", input={"x": 1})
    ]
    result_msg = {
        "role": "user",
        "content": [{"toolResult": {"toolUseId": "t1", "status": "success", "content": [{"text": "x" * 600}]}}],
    }
    [event] = normalize({"message": result_msg}, agent="a", session_id="s")
    assert isinstance(event, ToolResultEvent) and event.preview.endswith("…") and len(event.preview) == 501
    assert normalize({"event": {"raw": True}}, agent="a", session_id="s") == []


async def test_stream_tool_call_then_answer(runtime: Runtime) -> None:
    fakes.script(fakes.tool_use("echo", {"text": "ping"}), fakes.text("pong"))

    events = await collect(runtime, InvocationRequest(agent="echoer", prompt="hello", session_id="sess-0001"))

    kinds = [type(e).__name__ for e in events]
    assert kinds == ["StartEvent", "ToolUseEvent", "ToolResultEvent", "TextEvent", "ResultEvent"]
    assert events[0] == StartEvent(agent="echoer", session_id="sess-0001")
    tool_result = events[2]
    assert isinstance(tool_result, ToolResultEvent) and tool_result.preview == "echo:ping"
    result = events[-1]
    assert isinstance(result, ResultEvent)
    assert result.text == "pong" and result.stop_reason == "end_turn"
    assert result.metrics.tool_calls == 1 and result.metrics.total_tokens == 30


async def test_default_agent_and_generated_session_id(runtime: Runtime) -> None:
    fakes.script(fakes.text("ok"))
    events = await collect(runtime, InvocationRequest(prompt="hi"))
    start = events[0]
    assert isinstance(start, StartEvent) and start.agent == "echoer" and len(start.session_id) == 36


async def test_header_session_id_wins(runtime: Runtime) -> None:
    fakes.script(fakes.text("ok"))
    events = await collect(runtime, InvocationRequest(prompt="hi", session_id="payload-sid"), session_id="header-sid")
    assert isinstance(events[0], StartEvent) and events[0].session_id == "header-sid"


async def test_session_history_is_restored_between_invocations(runtime: Runtime) -> None:
    fakes.script(fakes.text("first"), fakes.text("second"))
    await collect(runtime, InvocationRequest(prompt="one", session_id="persist-1"))
    await collect(runtime, InvocationRequest(prompt="two", session_id="persist-1"))

    second_call_history = fakes.seen_messages()[1]
    texts = [block.get("text") for m in second_call_history for block in m["content"]]
    assert texts == ["one", "first", "two"]


async def test_unknown_agent_raises_before_streaming(runtime: Runtime) -> None:
    with pytest.raises(UnknownAgentError):
        await collect(runtime, InvocationRequest(agent="ghost", prompt="hi"))


async def test_model_failure_becomes_error_event(runtime: Runtime) -> None:
    fakes.script()  # no scripted response → the fake model raises
    events = await collect(runtime, InvocationRequest(prompt="hi"))
    assert isinstance(events[-1], ErrorEvent) and events[-1].error_type == "AssertionError"


async def test_invoke_returns_result(runtime: Runtime) -> None:
    fakes.script(fakes.text("done"))
    result = await runtime.invoker.invoke(InvocationRequest(prompt="hi"))
    assert isinstance(result, ResultEvent) and result.text == "done"

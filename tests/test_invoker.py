from __future__ import annotations

import base64
from pathlib import Path

import pytest

from strands_runtime.errors import SessionBusyError, UnknownAgentError
from strands_runtime.factory import SessionContext
from strands_runtime.invoke import InvocationRequest
from strands_runtime.invoke.invoker import _Run, decode_prompt
from strands_runtime.runtime import Runtime
from strands_runtime_client.events import (
    ErrorEvent,
    InterruptEvent,
    ReasoningEvent,
    ResultEvent,
    StartEvent,
    TextEvent,
    ToolResultEvent,
    ToolUseEvent,
)
from tests import fakes
from tests.conftest import write_agent, write_runtime

APPROVAL_TOOL = '''
from strands import ToolContext, tool


@tool(context=True)
def dangerous(action: str, tool_context: ToolContext) -> str:
    """Perform an action after human approval.

    Args:
        action: What to do.
    """
    answer = tool_context.interrupt("approve", reason=f"allow {action}?")
    return f"{action}: {answer}"
'''


async def collect(runtime: Runtime, request: InvocationRequest, **kwargs: object) -> list[object]:
    return [event async for event in runtime.invoker.stream(request, **kwargs)]  # type: ignore[arg-type]


def run_for(**options: object) -> _Run:
    return _Run(InvocationRequest(prompt="x", **options), agent_name="a", session=SessionContext("s"))  # type: ignore[arg-type]


def test_normalize_text_messages_and_reasoning_option() -> None:
    run = run_for()
    assert run.normalize({"data": "hi", "delta": {}}) == [TextEvent(text="hi")]
    assert run.normalize({"data": "", "delta": {}}) == []
    assert run.normalize({"reasoningText": "thinking", "delta": {}}) == []  # 既定では出さない
    assert run_for(include_reasoning=True).normalize({"reasoningText": "thinking"}) == [ReasoningEvent(text="thinking")]
    tool_use_msg = {
        "role": "assistant",
        "content": [{"toolUse": {"toolUseId": "t1", "name": "echo", "input": {"x": 1}}}],
    }
    assert run.normalize({"message": tool_use_msg}) == [ToolUseEvent(tool_use_id="t1", name="echo", input={"x": 1})]
    assert run.normalize({"event": {"raw": True}}) == []


def test_tool_result_preview_and_full_detail() -> None:
    content = [{"text": "x" * 600}, {"json": {"a": 1}}]
    result_msg = {
        "role": "user",
        "content": [{"toolResult": {"toolUseId": "t1", "status": "success", "content": content}}],
    }
    [event] = run_for().normalize({"message": result_msg})
    assert isinstance(event, ToolResultEvent)
    assert event.preview.endswith("…") and len(event.preview) == 501 and event.content is None
    [full] = run_for(tool_result_detail="full").normalize({"message": result_msg})
    assert isinstance(full, ToolResultEvent) and full.content == content


def test_decode_prompt_turns_base64_media_into_bytes() -> None:
    payload = [
        {"text": "what is this?"},
        {"image": {"format": "png", "source": {"bytes": base64.b64encode(b"\x89PNG").decode()}}},
    ]
    decoded = decode_prompt(payload)
    assert decoded[0] == {"text": "what is this?"}  # type: ignore[index]
    assert decoded[1]["image"]["source"]["bytes"] == b"\x89PNG"  # type: ignore[index]
    assert payload[1]["image"]["source"]["bytes"] != b"\x89PNG"  # 入力は書き換えない
    assert decode_prompt("plain") == "plain"


async def test_stream_tool_call_then_answer(runtime: Runtime) -> None:
    fakes.script(fakes.tool_use("echo", {"text": "ping"}), fakes.text("pong"))

    events = await collect(runtime, InvocationRequest(agent="echoer", prompt="hello", session_id="sess-0001"))

    kinds = [type(e).__name__ for e in events]
    collapsed = [k for i, k in enumerate(kinds) if i == 0 or k != kinds[i - 1]]
    assert collapsed == ["StartEvent", "ToolUseEvent", "ToolResultEvent", "TextEvent", "ResultEvent"]
    assert events[0] == StartEvent(agent="echoer", session_id="sess-0001")
    tool_result = events[2]
    assert isinstance(tool_result, ToolResultEvent) and tool_result.preview == "echo:ping"
    result = events[-1]
    assert isinstance(result, ResultEvent)
    assert result.text == "pong" and result.stop_reason == "end_turn"
    assert result.metrics.tool_calls == 1 and result.metrics.total_tokens == 30
    assert result.metrics.latency_ms >= 0


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


async def test_same_session_is_rejected_while_running(runtime: Runtime) -> None:
    fakes.script(fakes.text("slow"), fakes.text("second"))
    first = runtime.invoker.stream(InvocationRequest(prompt="hi", session_id="busy-1"))
    assert isinstance(await anext(first), StartEvent)
    with pytest.raises(SessionBusyError):
        await anext(runtime.invoker.stream(InvocationRequest(prompt="again", session_id="busy-1")))
    await first.aclose()
    # 放棄したストリームを閉じるとセッションは再び空く。
    events = await collect(runtime, InvocationRequest(prompt="again", session_id="busy-1"))
    assert isinstance(events[-1], ResultEvent)


async def test_model_failure_becomes_error_event(runtime: Runtime) -> None:
    fakes.script()  # 台本が空なので偽モデルは例外を投げる
    events = await collect(runtime, InvocationRequest(prompt="hi"))
    assert isinstance(events[-1], ErrorEvent) and events[-1].error_type == "AssertionError"
    assert sum(isinstance(e, ErrorEvent) for e in events) == 1


async def test_invoke_returns_result(runtime: Runtime) -> None:
    fakes.script(fakes.text("done"))
    result = await runtime.invoker.invoke(InvocationRequest(prompt="hi"))
    assert isinstance(result, ResultEvent) and result.text == "done"


async def test_timeout_cancels_and_reports_timeout(tmp_path: Path) -> None:
    write_agent(tmp_path / "agents", "slow", toml_extra="[limits]\ntimeout_seconds = 0.3\n")
    runtime = Runtime.load(write_runtime(tmp_path))
    fakes.script(fakes.text("a" * 20), fakes.text("never"))
    fakes.set_chunk_delay(0.25)
    try:
        events = await collect(runtime, InvocationRequest(agent="slow", prompt="hi", session_id="t-1"))
    finally:
        fakes.set_chunk_delay(0.0)
    final = events[-1]
    assert isinstance(final, ResultEvent | ErrorEvent)
    if isinstance(final, ResultEvent):
        assert final.stop_reason == "timeout"
    else:
        assert final.error_type == "Timeout"


async def test_structured_output_has_its_own_stop_reason(tmp_path: Path) -> None:
    write_agent(tmp_path / "agents", "a", toml_extra='[structured_output]\nmodel = "tests.schemas:Answer"\n')
    runtime = Runtime.load(write_runtime(tmp_path))
    # Strands は構造化出力を、スキーマ名のツール呼び出しとしてモデルに求める。
    fakes.script(fakes.tool_use("Answer", {"answer": "42"}))
    result = await runtime.invoker.invoke(InvocationRequest(agent="a", prompt="hi"))
    assert isinstance(result, ResultEvent)
    assert result.stop_reason == "structured_output" and result.structured_output == {"answer": "42"}


async def test_interrupt_and_resume(tmp_path: Path) -> None:
    directory = write_agent(
        tmp_path / "agents", "hitl", with_tool=False, toml_extra='tools = ["./tools/dangerous.py"]\n'
    )
    (directory / "tools").mkdir()
    (directory / "tools" / "dangerous.py").write_text(APPROVAL_TOOL, encoding="utf-8")
    runtime = Runtime.load(write_runtime(tmp_path))
    fakes.script(fakes.tool_use("dangerous", {"action": "deploy"}), fakes.text("deployed"))

    first = await collect(runtime, InvocationRequest(agent="hitl", prompt="deploy it", session_id="hitl-1"))
    interrupt = next(e for e in first if isinstance(e, InterruptEvent))
    result = first[-1]
    assert isinstance(result, ResultEvent) and result.stop_reason == "interrupt"
    assert interrupt.interrupts[0].name == "approve" and interrupt.interrupts[0].reason == "allow deploy?"

    resume = InvocationRequest(
        agent="hitl",
        prompt=[{"interruptResponse": {"interruptId": interrupt.interrupts[0].id, "response": "yes"}}],
        session_id="hitl-1",
    )
    second = await collect(runtime, resume)
    tool_result = next(e for e in second if isinstance(e, ToolResultEvent))
    assert tool_result.preview == "deploy: yes"
    assert isinstance(second[-1], ResultEvent) and second[-1].text == "deployed"


async def test_context_injection_adds_datetime_and_actor(tmp_path: Path) -> None:
    write_agent(
        tmp_path / "agents", "ctx", toml_extra='[context]\ninject = ["datetime", "actor"]\ntimezone = "Asia/Tokyo"\n'
    )
    runtime = Runtime.load(write_runtime(tmp_path))
    fakes.script(fakes.text("ok"))
    await collect(runtime, InvocationRequest(agent="ctx", prompt="hi", actor_id="user-42"))
    seen = str(fakes.seen_messages()[0])
    assert "<current_datetime>" in seen and "+09:00" in seen and "<actor_id>user-42</actor_id>" in seen


async def test_context_injection_resolves_file_references_relative_to_the_agent(tmp_path: Path) -> None:
    agent_dir = write_agent(
        tmp_path / "agents", "ctx", toml_extra='[context]\ninject = ["./injectors.py:render_turn"]\n'
    )
    (agent_dir / "injectors.py").write_text(
        "def render_turn(context):\n"
        "    return f\"<turn>{sum(1 for m in context.messages if m.get('role') == 'user')}</turn>\"\n",
        encoding="utf-8",
    )
    runtime = Runtime.load(write_runtime(tmp_path))
    fakes.script(fakes.text("ok"))
    await collect(runtime, InvocationRequest(agent="ctx", prompt="hi"))
    assert "<turn>1</turn>" in str(fakes.seen_messages()[0])


async def test_usage_is_aggregated_across_subagents(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    write_agent(agents, "parent", toml_extra='subagents = ["child"]\n', with_tool=False)
    write_agent(agents, "child", with_tool=False)
    runtime = Runtime.load(write_runtime(tmp_path))
    # parent が child を呼び（child 内でモデル呼び出し 1 回）、その後に回答する。計 3 回のモデル呼び出し、各 15 トークン。
    fakes.script(fakes.tool_use("child", {"input": "hello"}), fakes.text("child says hi"), fakes.text("done"))

    result = await runtime.invoker.invoke(InvocationRequest(agent="parent", prompt="go", session_id="usage-1"))

    assert isinstance(result, ResultEvent)
    assert result.metrics.total_tokens == 45 and result.metrics.input_tokens == 30
    assert set(result.metrics.agents) == {"parent", "child"}
    assert result.metrics.agents["child"].invocations == 1 and result.metrics.agents["child"].total_tokens == 15
    assert result.metrics.agents["parent"].total_tokens == 30
    assert result.metrics.agents["parent"].model == "scripted/fake"
    assert result.metrics.model_failures == 0


async def test_fallback_model_takes_over_when_primary_fails(tmp_path: Path) -> None:
    write_agent(
        tmp_path / "agents",
        "a",
        with_tool=False,
        model_table='[model]\nmodule = "tests.fakes:make_failing_model"\nfallbacks = [{ module = "tests.fakes:make_model" }]\n',
    )
    runtime = Runtime.load(write_runtime(tmp_path))
    fakes.script(fakes.text("from the fallback"))

    result = await runtime.invoker.invoke(InvocationRequest(agent="a", prompt="hi", session_id="fb-1"))

    assert isinstance(result, ResultEvent), result
    assert result.text == "from the fallback"
    assert result.metrics.model_failures >= 1
    assert result.metrics.agents["a"].model.startswith("router(")

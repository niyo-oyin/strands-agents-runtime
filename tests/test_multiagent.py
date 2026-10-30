"""Graph / Swarm（設計文書 §8）。偽モデルで Strands のオーケストレーターを実際に動かす。"""

from __future__ import annotations

import asyncio
import textwrap
import threading
from pathlib import Path

import pytest

from strands_runtime.errors import DefinitionError, InvalidRequestError
from strands_runtime.invoke import InvocationRequest
from strands_runtime.runtime import Runtime
from strands_runtime_client import Message
from strands_runtime_client.events import (
    ErrorEvent,
    HandoffEvent,
    NodeStartEvent,
    NodeStopEvent,
    ResultEvent,
    TextEvent,
    ToolUseEvent,
)
from tests import fakes
from tests.conftest import write_agent, write_runtime


def write_multiagent(agents_dir: Path, name: str, body: str) -> Path:
    directory = agents_dir / name
    directory.mkdir(parents=True)
    (directory / "agent.toml").write_text(textwrap.dedent(body), encoding="utf-8")
    return directory


async def collect(runtime: Runtime, request: InvocationRequest, **kwargs: object) -> list[object]:
    return [event async for event in runtime.invoker.stream(request, **kwargs)]  # type: ignore[arg-type]


def types(events: list[object]) -> list[str]:
    return [e.type for e in events]  # type: ignore[attr-defined]


def pipeline(tmp_path: Path, *, extra: str = "") -> Runtime:
    agents = tmp_path / "agents"
    write_agent(agents, "planner", with_tool=False)
    write_agent(agents, "writer", with_tool=False)
    write_multiagent(
        agents,
        "flow",
        f"""
        name = "flow"
        kind = "graph"
        description = "plan then write"
        nodes = ["planner", "writer"]
        {extra}
        [[edges]]
        from = "planner"
        to = "writer"
        """,
    )
    return Runtime.load(write_runtime(tmp_path, 'default_agent = "flow"'))


async def test_graph_accepts_single_user_message(tmp_path):
    runtime = pipeline(tmp_path)
    fakes.script(fakes.text("plan"), fakes.text("draft"))
    result = await runtime.invoker.invoke(
        InvocationRequest(messages=[Message(role="user", content="write something")], session_id="g-message")
    )
    assert isinstance(result, ResultEvent) and result.text == "draft"
    assert fakes.seen_messages()[0][0]["content"] == [{"text": "write something"}]


@pytest.mark.parametrize(
    "options,match",
    [
        ({"prompt": "hi", "model": "openai/deployment"}, "model override"),
        ({"messages": [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]}, "single user"),
        ({"messages": [{"role": "assistant", "content": "a"}]}, "single user"),
    ],
)
async def test_graph_rejects_unsupported_inputs_before_start(tmp_path, options, match):
    runtime = pipeline(tmp_path)
    stream = runtime.invoker.stream(InvocationRequest.model_validate(options))
    with pytest.raises(InvalidRequestError, match=match):
        await anext(stream)


async def test_graph_runs_nodes_in_order_and_reports_node_events(tmp_path: Path) -> None:
    runtime = pipeline(tmp_path)
    fakes.script(fakes.text("the plan"), fakes.text("the draft"))

    events = await collect(runtime, InvocationRequest(prompt="write something", session_id="g-1"))

    starts = [e for e in events if isinstance(e, NodeStartEvent)]
    stops = [e for e in events if isinstance(e, NodeStopEvent)]
    assert [s.node_id for s in starts] == ["planner", "writer"] and starts[0].agent == "planner"
    assert [(s.node_id, s.status) for s in stops] == [("planner", "completed"), ("writer", "completed")]
    assert stops[0].preview == "the plan"
    # Graph はバッチ間の遷移を handoff として出す。
    handoffs = [e for e in events if isinstance(e, HandoffEvent)]
    assert handoffs and handoffs[0].from_nodes == ["planner"] and handoffs[0].to_nodes == ["writer"]
    # ノード内の本文は差分で、agent / parent が付く。
    node_text = "".join(e.text for e in events if isinstance(e, TextEvent) and e.parent == "writer")
    assert node_text == "the draft" and all(e.agent for e in events if isinstance(e, TextEvent))

    result = events[-1]
    assert isinstance(result, ResultEvent)
    assert result.stop_reason == "end_turn" and result.text == "the draft"  # output 省略時は最後に完了したノード
    assert result.nodes is not None and set(result.nodes) == {"planner", "writer"}
    assert result.nodes["planner"].agent == "planner" and result.nodes["planner"].text == "the plan"
    assert result.metrics.total_tokens == 30 and set(result.metrics.agents) == {"planner", "writer"}
    # 2 回目の呼び出しは最初から実行し直す（会話は持ち越さない）。
    fakes.script(fakes.text("plan 2"), fakes.text("draft 2"))
    second = await runtime.invoker.invoke(InvocationRequest(prompt="again", session_id="g-1"))
    assert isinstance(second, ResultEvent) and second.text == "draft 2"
    assert len(fakes.seen_messages()[0]) == 1  # 前回の会話は含まれない


async def test_graph_output_node_selects_the_text(tmp_path: Path) -> None:
    runtime = pipeline(tmp_path, extra='output = "planner"')
    fakes.script(fakes.text("the plan"), fakes.text("the draft"))
    result = await runtime.invoker.invoke(InvocationRequest(prompt="go"))
    assert isinstance(result, ResultEvent) and result.text == "the plan"


async def test_graph_conditions_and_wait_for_all(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    for name in ("split", "left", "right", "merge"):
        write_agent(agents, name, with_tool=False)
    flow = write_multiagent(
        agents,
        "fan",
        """
        name = "fan"
        kind = "graph"
        [[nodes]]
        agent = "split"
        [[nodes]]
        agent = "left"
        [[nodes]]
        agent = "right"
        [[nodes]]
        agent = "merge"
        wait_for = "all"
        [[edges]]
        from = "split"
        to = "left"
        [[edges]]
        from = "split"
        to = "right"
        condition = "./conditions.py:always"
        [[edges]]
        from = "left"
        to = "merge"
        [[edges]]
        from = "right"
        to = "merge"
        """,
    )
    (flow / "conditions.py").write_text("def always(state):\n    return True\n", encoding="utf-8")
    runtime = Runtime.load(write_runtime(tmp_path))
    fakes.script(fakes.text("s"), fakes.text("l"), fakes.text("r"), fakes.text("m"))

    events = await collect(runtime, InvocationRequest(agent="fan", prompt="go"))

    stops = [e.node_id for e in events if isinstance(e, NodeStopEvent)]
    assert stops[0] == "split" and stops[-1] == "merge" and set(stops[1:3]) == {"left", "right"}
    starts = [e.node_id for e in events if isinstance(e, NodeStartEvent)]
    assert starts.index("merge") > max(stops.index("left"), stops.index("right"))  # 合流は両方の完了を待つ
    result = events[-1]
    assert isinstance(result, ResultEvent) and result.nodes is not None and len(result.nodes) == 4
    assert result.nodes["merge"].execution_count == 1


async def test_graph_limit_becomes_graph_failed_error(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    write_agent(agents, "ping", with_tool=False)
    write_agent(agents, "pong", with_tool=False)
    write_multiagent(
        agents,
        "loop",
        """
        name = "loop"
        kind = "graph"
        nodes = ["ping", "pong"]
        entry = ["ping"]
        reset_on_revisit = true
        [[edges]]
        from = "ping"
        to = "pong"
        [[edges]]
        from = "pong"
        to = "ping"
        [limits]
        node_executions = 3
        """,
    )
    runtime = Runtime.load(write_runtime(tmp_path))
    fakes.script(*[fakes.text(str(i)) for i in range(10)])
    events = await collect(runtime, InvocationRequest(agent="loop", prompt="go"))
    final = events[-1]
    assert isinstance(final, ErrorEvent) and final.error_type == "GraphFailed", final
    assert "limits" in final.message
    assert len([e for e in events if isinstance(e, NodeStopEvent)]) >= 3


async def test_graph_node_exception_becomes_graph_failed_error(tmp_path: Path) -> None:
    runtime = pipeline(tmp_path)
    fakes.script(fakes.text("the plan"))  # writer の分の台本がないので偽モデルが例外を投げる
    events = await collect(runtime, InvocationRequest(prompt="go"))
    final = events[-1]
    assert (
        isinstance(final, ErrorEvent) and final.error_type == "GraphFailed" and "no scripted response" in final.message
    )


async def test_graph_cancellation_is_propagated_to_nodes(tmp_path: Path) -> None:
    runtime = pipeline(tmp_path)
    fakes.script(fakes.text("x" * 200), fakes.text("never"))
    fakes.set_chunk_delay(0.02)
    cancel = threading.Event()
    try:
        stream = runtime.invoker.stream(InvocationRequest(prompt="go"), cancel_signal=cancel)
        events: list[object] = [await anext(stream)]
        await asyncio.sleep(0.1)
        cancel.set()
        events.extend([e async for e in stream])
    finally:
        fakes.set_chunk_delay(0.0)
    final = events[-1]
    assert isinstance(final, ResultEvent) and final.stop_reason == "cancelled", final
    assert "writer" not in [e.node_id for e in events if isinstance(e, NodeStartEvent)]


async def test_swarm_handoff(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    write_agent(agents, "triage", with_tool=False, prompt="Route requests.")
    write_agent(agents, "billing", with_tool=False, prompt="Handle billing.")
    write_multiagent(
        agents,
        "support",
        """
        name = "support"
        kind = "swarm"
        nodes = ["triage", "billing"]
        entry = "triage"
        [limits]
        handoffs = 5
        """,
    )
    runtime = Runtime.load(write_runtime(tmp_path))
    # 引き継ぎは、ツールを呼んだエージェントが自分のターンを終えてから起きる（ツール結果の後にもう 1 回モデルが呼ばれる）。
    fakes.script(
        fakes.tool_use("handoff_to_agent", {"agent_name": "billing", "message": "refund request"}),
        fakes.text("passing this to billing"),
        fakes.text("refund issued"),
    )

    events = await collect(runtime, InvocationRequest(agent="support", prompt="I want a refund", session_id="s-1"))

    assert types(events)[0] == "start"
    tool_uses = [e for e in events if isinstance(e, ToolUseEvent)]
    assert tool_uses and tool_uses[0].name == "handoff_to_agent" and tool_uses[0].parent == "triage"
    handoff = next(e for e in events if isinstance(e, HandoffEvent))
    assert handoff.from_nodes == ["triage"] and handoff.to_nodes == ["billing"] and handoff.message == "refund request"
    assert [e.node_id for e in events if isinstance(e, NodeStartEvent)] == ["triage", "billing"]
    result = events[-1]
    assert isinstance(result, ResultEvent), events[-3:]
    assert result.stop_reason == "end_turn" and result.text == "refund issued"
    assert result.nodes is not None and result.nodes["billing"].status == "completed"
    # Swarm のノードには、他のエージェントの説明を含むコンテキストが入力として渡る。
    assert "Other agents available" in str(fakes.seen_messages()[0])


async def test_registry_rejects_invalid_compositions(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    write_agent(agents, "solo", with_tool=False)
    write_multiagent(agents, "g", 'name = "g"\nkind = "graph"\nnodes = ["solo"]\n')
    write_multiagent(agents, "s", 'name = "s"\nkind = "swarm"\nnodes = ["g"]\n')  # swarm のノードに graph は置けない
    with pytest.raises(DefinitionError, match="swarm nodes must be kind"):
        Runtime.load(write_runtime(tmp_path))

    (agents / "s").joinpath("agent.toml").unlink()
    (agents / "s").rmdir()
    write_agent(agents, "parent", toml_extra='subagents = ["g"]\n', with_tool=False)  # subagents にも置けない
    with pytest.raises(DefinitionError, match="subagents must be kind"):
        Runtime.load(write_runtime(tmp_path))


async def test_graph_schema_errors(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    write_agent(agents, "a", with_tool=False)
    write_agent(agents, "b", with_tool=False)
    bad = write_multiagent(
        agents, "bad", 'name = "bad"\nkind = "graph"\nnodes = ["a"]\n[[edges]]\nfrom = "a"\nto = "zzz"\n'
    )
    with pytest.raises(DefinitionError, match="unknown node"):
        Runtime.load(write_runtime(tmp_path))
    # 全ノードに入力エッジがある（循環だけ）のグラフは entry が必須。
    (bad / "agent.toml").write_text(
        'name = "bad"\nkind = "graph"\nnodes = ["a", "b"]\n[[edges]]\nfrom = "a"\nto = "b"\n[[edges]]\nfrom = "b"\nto = "a"\n',
        encoding="utf-8",
    )
    with pytest.raises(DefinitionError, match="entry is required"):
        Runtime.load(write_runtime(tmp_path))


async def test_graph_in_job_mode_journals_node_events(tmp_path: Path) -> None:
    runtime = pipeline(tmp_path)
    fakes.script(fakes.text("the plan"), fakes.text("the draft"))
    job = await runtime.jobs.submit(InvocationRequest(prompt="go", mode="job", session_id="gj-1"))
    events = [e async for e in runtime.jobs.follow("gj-1", job.job_id)]
    assert types(events) == [
        "start",
        "node_start",
        "text",
        "node_stop",
        "handoff",
        "node_start",
        "text",
        "node_stop",
        "result",
    ], types(events)
    status = await runtime.jobs.status("gj-1")
    assert status.state == "completed" and status.last_event is not None and status.last_event.type == "result"

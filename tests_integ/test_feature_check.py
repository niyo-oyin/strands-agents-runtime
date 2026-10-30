"""examples/feature-check のエージェントを実モデル（OpenRouter）で動かす確認。OPENROUTER_API_KEY がなければスキップする。

実行方法:  uv run --env-file .env pytest tests_integ -q
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from strands_runtime.invoke import InvocationRequest
from strands_runtime.runtime import Runtime
from strands_runtime_client.events import (
    HandoffEvent,
    NodeStartEvent,
    NodeStopEvent,
    ResultEvent,
    ToolResultEvent,
    ToolUseEvent,
)

pytestmark = pytest.mark.skipif(not os.environ.get("OPENROUTER_API_KEY"), reason="OPENROUTER_API_KEY is not set")

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def runtime() -> Runtime:
    return Runtime.load(ROOT / "examples" / "feature-check" / "runtime.toml")


async def run(runtime: Runtime, agent: str, prompt: str, **options: object) -> list[object]:
    request = InvocationRequest(agent=agent, prompt=prompt, **options)  # type: ignore[arg-type]
    return [event async for event in runtime.invoker.stream(request)]


def result_of(events: list[object]) -> ResultEvent:
    final = events[-1]
    assert isinstance(final, ResultEvent), final
    return final


async def test_skill_is_activated_and_followed(runtime: Runtime) -> None:
    events = await run(runtime, "skilled", "俳句スタイルで、冬の朝について答えてください。")
    assert any(isinstance(e, ToolUseEvent) and e.name == "skills" for e in events)
    assert "詠み人: strands" in result_of(events).text


async def test_mcp_tools_are_called(runtime: Runtime) -> None:
    events = await run(runtime, "mcp-user", "12 と 34 を掛けた結果を教えてください。")
    assert any(isinstance(e, ToolUseEvent) and e.name == "multiply" for e in events)
    assert "408" in result_of(events).text


async def test_structured_output(runtime: Runtime) -> None:
    events = await run(runtime, "extractor", "山田花子（hanako@example.org、Beta 社）が料金について質問。")
    result = result_of(events)
    assert result.stop_reason == "structured_output"
    assert result.structured_output and result.structured_output["name"] == "山田花子"


async def test_turn_limit_stops_after_tool(runtime: Runtime) -> None:
    events = await run(runtime, "limited", "サイコロを振ってください。")
    assert result_of(events).stop_reason == "limit_turns"


async def test_invocation_state_reaches_tools_and_full_results(runtime: Runtime) -> None:
    events = await run(
        runtime, "tenant-aware", "私はどのテナント？", invocation_state={"tenant": "acme"}, tool_result_detail="full"
    )
    tool_result = next(e for e in events if isinstance(e, ToolResultEvent))
    assert tool_result.content == [{"text": "tenant=acme"}]
    assert "acme" in result_of(events).text


async def test_fallback_to_second_model_and_usage(runtime: Runtime) -> None:
    events = await run(runtime, "fallback", "1 たす 1 は？数字だけ。")
    result = result_of(events)
    assert "2" in result.text
    assert result.metrics.model_failures >= 1
    assert result.metrics.total_tokens > 0 and "fallback" in result.metrics.agents


async def test_graph_runs_nodes_and_reports_node_results(runtime: Runtime) -> None:
    events = await run(runtime, "writing-flow", "テーマ: 朝の散歩")
    node_ids = [e.node_id for e in events if isinstance(e, NodeStopEvent)]
    assert node_ids[:2] == ["outliner", "drafter"] and "reviewer" in node_ids
    result = result_of(events)
    assert result.nodes is not None and result.nodes["outliner"].status == "completed"
    assert result.text  # output = drafter


async def test_swarm_hands_off_to_the_right_agent(runtime: Runtime) -> None:
    events = await run(runtime, "helpdesk", "先月二重に請求されています。返金してもらえますか？")
    handoffs = [e for e in events if isinstance(e, HandoffEvent)]
    assert handoffs and handoffs[0].to_nodes == ["billing"]
    result = result_of(events)
    assert result.nodes is not None and result.nodes["billing"].status == "completed"


async def test_nested_swarm_inside_graph(runtime: Runtime) -> None:
    events = await run(runtime, "nested-flow", "請求書が届かないので再発行してほしい")
    inner = [e for e in events if isinstance(e, NodeStartEvent) and e.parent == "helpdesk"]
    assert [e.node_id for e in inner][:1] == ["triage"]
    result = result_of(events)
    assert result.nodes is not None and set(result.nodes) == {"helpdesk", "summarizer"}
    assert result.text.startswith("要約")

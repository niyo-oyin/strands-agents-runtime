"""1 回の呼び出しにおける、エージェントツリー全体のトークン集計。

Strands の使用量は ``Agent`` オブジェクト単位（``agent.event_loop_metrics.accumulated_usage``）で持たれ、
サブエージェントは別オブジェクトとしてツール呼び出しのたびに実行されるため、ルートの値には子の分が含まれない。
このフックを factory がツリー内の全エージェントに付け、呼び出し前後の差分をエージェント名ごとに足し合わせる。
差分方式なので、同じ子が何度呼ばれても、入れ子になっていても、合計が正しくなる。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from strands.hooks import AfterInvocationEvent, AfterModelCallEvent, BeforeInvocationEvent, HookProvider, HookRegistry

from strands_runtime.factory.models import model_label

# Strands の ``Usage`` のキー。キャッシュ関連は対応プロバイダーでしか付かない。
_USAGE_KEYS = ("inputTokens", "outputTokens", "totalTokens", "cacheReadInputTokens", "cacheWriteInputTokens")


@dataclass
class AgentUsage:
    """エージェント 1 つ分の集計。"""

    model: str
    invocations: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def add(self, usage: dict[str, int]) -> None:
        self.input_tokens += usage.get("inputTokens", 0)
        self.output_tokens += usage.get("outputTokens", 0)
        self.total_tokens += usage.get("totalTokens", 0)
        self.cache_read_tokens += usage.get("cacheReadInputTokens", 0)
        self.cache_write_tokens += usage.get("cacheWriteInputTokens", 0)


@dataclass
class UsageCollector(HookProvider):
    """呼び出しごとに 1 つ作り、``AgentFactory.build(hooks=[collector])`` で渡す。"""

    agents: dict[str, AgentUsage] = field(default_factory=dict)
    model_failures: int = 0
    # 呼び出し開始時点の累積値。Agent オブジェクトの id をキーにして、終了時に差分を取る。
    _before: dict[int, dict[str, int]] = field(default_factory=dict)

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeInvocationEvent, self._on_before)
        registry.add_callback(AfterInvocationEvent, self._on_after)
        registry.add_callback(AfterModelCallEvent, self._on_model_call)

    def _on_before(self, event: BeforeInvocationEvent) -> None:
        self._before[id(event.agent)] = _snapshot(event.agent)

    def _on_after(self, event: AfterInvocationEvent) -> None:
        agent = event.agent
        before = self._before.pop(id(agent), {})
        after = _snapshot(agent)
        delta = {key: after.get(key, 0) - before.get(key, 0) for key in _USAGE_KEYS}
        # ``Agent`` は ``model`` に渡された ``ModelRouter`` を ``_model_router`` に保持し、``.model`` には既定モデルを置く。
        # 表示にはルーターの方を使い、フォールバック構成であることが分かるようにする。
        router = getattr(agent, "_model_router", None)
        entry = self.agents.setdefault(agent.name, AgentUsage(model=model_label(router or agent.model)))
        entry.invocations += 1
        entry.add(delta)

    def _on_model_call(self, event: AfterModelCallEvent) -> None:
        # 例外付きで終わったモデル呼び出しを数える。フォールバックがあれば、この回数だけ別候補に切り替わっている。
        if event.exception is not None:
            self.model_failures += 1

    def totals(self) -> AgentUsage:
        """全エージェントの合計。"""
        total = AgentUsage(model="*")
        for entry in self.agents.values():
            total.invocations += entry.invocations
            total.input_tokens += entry.input_tokens
            total.output_tokens += entry.output_tokens
            total.total_tokens += entry.total_tokens
            total.cache_read_tokens += entry.cache_read_tokens
            total.cache_write_tokens += entry.cache_write_tokens
        return total


def _snapshot(agent: Any) -> dict[str, int]:
    """エージェントの累積使用量を、欠けているキーを 0 で埋めた辞書として写す。"""
    usage = agent.event_loop_metrics.accumulated_usage
    return {key: int(usage.get(key, 0)) for key in _USAGE_KEYS}

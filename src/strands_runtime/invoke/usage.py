"""Token accounting across the agent tree of one invocation.

Strands keeps usage per ``Agent`` object (``agent.event_loop_metrics.accumulated_usage``) and a subagent is a
separate object invoked once per tool call, so the root's metrics never include its children. This hook is
attached to every agent the factory builds for a request; it snapshots usage before each invocation and adds
the delta afterwards, keyed by agent name, which makes the totals correct for nested and repeated calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from strands.hooks import AfterInvocationEvent, AfterModelCallEvent, BeforeInvocationEvent, HookProvider, HookRegistry

from strands_runtime.factory.models import model_label

_USAGE_KEYS = ("inputTokens", "outputTokens", "totalTokens", "cacheReadInputTokens", "cacheWriteInputTokens")


@dataclass
class AgentUsage:
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
    """One per invocation; pass it to ``AgentFactory.build(hooks=[collector])``."""

    agents: dict[str, AgentUsage] = field(default_factory=dict)
    model_failures: int = 0
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
        # ``Agent`` keeps a router passed as ``model`` on ``_model_router`` and exposes the default model as ``.model``.
        router = getattr(agent, "_model_router", None)
        entry = self.agents.setdefault(agent.name, AgentUsage(model=model_label(router or agent.model)))
        entry.invocations += 1
        entry.add(delta)

    def _on_model_call(self, event: AfterModelCallEvent) -> None:
        if event.exception is not None:
            self.model_failures += 1

    def totals(self) -> AgentUsage:
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
    usage = agent.event_loop_metrics.accumulated_usage
    return {key: int(usage.get(key, 0)) for key in _USAGE_KEYS}

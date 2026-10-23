"""応答を台本で与える ``strands.models.Model``。プロバイダーなしで本物のエージェントループを回すために使う。

テスト対象のエージェント定義は ``[model] module = "tests.fakes:make_model"`` でこのモジュールを指し、
テスト側は呼び出す前に :func:`script` で台本を設定する。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any

from pydantic import BaseModel
from strands.models import Model
from strands.types.content import Message, Messages
from strands.types.tools import ToolSpec

_SCRIPT: list[Message] = []
_CALLS = 0
_SEEN: list[Messages] = []
_CHUNK_DELAY = 0.0


def script(*responses: Message) -> None:
    """これ以降のモデル呼び出しが返すアシスタントメッセージを、呼び出し 1 回につき 1 つずつ設定する。"""
    global _CALLS
    _SCRIPT[:] = list(responses)
    _SEEN.clear()
    _CALLS = 0


def set_chunk_delay(seconds: float) -> None:
    """ストリームの各チャンクの前にこの秒数だけ待つ（タイムアウトとキャンセルを試すため）。"""
    global _CHUNK_DELAY
    _CHUNK_DELAY = seconds


def seen_messages() -> list[Messages]:
    """各呼び出しでモデルが受け取ったメッセージ履歴（セッション復元を観測するため）。"""
    return list(_SEEN)


def text(content: str) -> Message:
    return {"role": "assistant", "content": [{"text": content}]}


def tool_use(name: str, tool_input: dict[str, Any], tool_use_id: str = "tu-1") -> Message:
    return {
        "role": "assistant",
        "content": [{"toolUse": {"toolUseId": tool_use_id, "name": name, "input": tool_input}}],
    }


def make_model() -> ScriptedModel:
    return ScriptedModel()


def make_failing_model() -> FailingModel:
    return FailingModel()


class FailingModel(Model):
    """毎回例外を投げるモデル。``[model] fallbacks`` のルーターが次の候補へ進むことを確かめるために使う。"""

    def __init__(self) -> None:
        self.config: dict[str, Any] = {"model_id": "failing/primary", "context_window_limit": 200_000}

    def update_config(self, **model_config: Any) -> None:
        self.config.update(model_config)

    def get_config(self) -> dict[str, Any]:
        return self.config

    async def structured_output(
        self, output_model: type[BaseModel], prompt: Messages, system_prompt: str | None = None, **kwargs: Any
    ) -> AsyncGenerator[dict[str, Any]]:
        raise RuntimeError("primary model is down")
        yield {}  # pragma: no cover - この関数を非同期ジェネレーターにするためだけの行

    async def stream(
        self,
        messages: Messages,
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any]]:
        raise RuntimeError("primary model is down")
        yield {}  # pragma: no cover - この関数を非同期ジェネレーターにするためだけの行


class ScriptedModel(Model):
    def __init__(self) -> None:
        self.config: dict[str, Any] = {"model_id": "scripted/fake", "context_window_limit": 200_000}

    def update_config(self, **model_config: Any) -> None:
        self.config.update(model_config)

    def get_config(self) -> dict[str, Any]:
        return self.config

    async def structured_output(
        self, output_model: type[BaseModel], prompt: Messages, system_prompt: str | None = None, **kwargs: Any
    ) -> AsyncGenerator[dict[str, Any]]:
        yield {"output": output_model()}

    async def stream(
        self,
        messages: Messages,
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any]]:
        global _CALLS
        _SEEN.append([dict(m) for m in messages])
        if len(_SCRIPT) <= _CALLS:
            raise AssertionError(f"ScriptedModel: no scripted response for call #{_CALLS + 1}")
        response = _SCRIPT[_CALLS]
        _CALLS += 1

        stop_reason = "end_turn"
        yield {"messageStart": {"role": "assistant"}}
        for block in response["content"]:
            if "text" in block:
                yield {"contentBlockStart": {"start": {}}}
                for char in block["text"]:
                    if _CHUNK_DELAY:
                        await asyncio.sleep(_CHUNK_DELAY)
                    yield {"contentBlockDelta": {"delta": {"text": char}}}
                yield {"contentBlockStop": {}}
            if "toolUse" in block:
                stop_reason = "tool_use"
                tu = block["toolUse"]
                yield {"contentBlockStart": {"start": {"toolUse": {"name": tu["name"], "toolUseId": tu["toolUseId"]}}}}
                yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(tu["input"])}}}}
                yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": stop_reason}}
        yield {
            "metadata": {
                "usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
                "metrics": {"latencyMs": 3},
            }
        }

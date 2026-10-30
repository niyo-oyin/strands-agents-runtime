"""Pydantic モデルと通信上の JSON Schema の変換。プロバイダー SDK は不要。"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

from pydantic import BaseModel

from strands_runtime_client.events import ResultEvent
from strands_runtime_client.inputs import ResponseFormat

ResponseFormatInput = ResponseFormat | dict[str, Any] | type[BaseModel] | None


def response_model(value: ResponseFormatInput) -> type[BaseModel] | None:
    if not isinstance(value, type):
        return None
    if not issubclass(value, BaseModel) or value is BaseModel:
        raise TypeError("response_format must be a Pydantic BaseModel subclass or a response format object")
    return value


def wire_response_format(value: ResponseFormatInput) -> ResponseFormat | dict[str, Any] | None:
    model = response_model(value)
    if model is None:
        # クラス以外の入力形式は共通の InvocationInput で検証する。
        return value  # type: ignore[return-value]
    schema = model.model_json_schema()
    # 再帰モデルでは、ルートが $defs 内の object への参照になることがある。
    if "$ref" in schema:
        ref = schema["$ref"]
        target: Any = schema
        if not isinstance(ref, str) or not ref.startswith("#/"):
            raise ValueError("response_format requires a local JSON Schema reference")
        for part in ref[2:].split("/"):
            target = target[part.replace("~1", "/").replace("~0", "~")]
        schema = {**target, **{key: item for key, item in schema.items() if key != "$ref"}}
    if schema.get("type") != "object" or "anyOf" in schema:
        raise ValueError("response_format requires a Pydantic model whose JSON Schema root is an object")
    _strict_objects(schema)
    name = re.sub(r"[^a-zA-Z0-9_-]", "_", model.__name__)[:64]
    return {"type": "json_schema", "json_schema": {"name": name, "schema": schema, "strict": True}}


def _strict_objects(schema: dict[str, Any]) -> None:
    """固定フィールドの object を strict 出力用にする。任意キーの辞書は黙って狭めず拒否する。"""
    schema.pop("default", None)
    if schema.get("type") == "object":
        if schema.get("additionalProperties", False) is not False:
            raise ValueError(
                "Pydantic response_format requires fixed object fields; dict fields and extra='allow' "
                "are not supported in strict mode. Pass an explicit response format object instead."
            )
        schema["additionalProperties"] = False
        schema["required"] = list(schema.get("properties", {}))
    for key in ("properties", "$defs", "definitions"):
        for child in schema.get(key, {}).values():
            _strict_objects(child)
    if isinstance(schema.get("items"), dict):
        _strict_objects(schema["items"])
    for key in ("anyOf", "allOf", "oneOf", "prefixItems"):
        for child in schema.get(key, []):
            _strict_objects(child)


def parse_result(result: ResultEvent, model: type[BaseModel] | None) -> ResultEvent:
    """正常完了した JSON を検証する。中断・上限到達などの結果は解析せずそのまま返す。"""
    if model is None or result.stop_reason != "end_turn" or isinstance(result.parsed, model):
        return result
    parsed = model.model_validate_json(result.text)
    result = result.model_copy()
    result._parsed = parsed
    return result


def parse_events(events: Iterator[Any], model: type[BaseModel] | None) -> Iterator[Any]:
    for event in events:
        yield parse_result(event, model) if isinstance(event, ResultEvent) else event

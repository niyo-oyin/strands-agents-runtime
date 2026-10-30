"""リクエストの生成条件をプロバイダーの設定へ変換する。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from strands import Agent
from strands.hooks import AfterModelCallEvent, BeforeModelCallEvent, HookOrder
from strands.models import Model, ModelRouter

from strands_runtime.definitions.schema import ModelCandidate, ModelDefaults, ModelSpec
from strands_runtime.errors import InvalidRequestError
from strands_runtime_client.inputs import FunctionToolChoice, GenerationOptions

_RESPONSES = {"openai", "bedrock-mantle"}
_CHAT = {"openai-chat", "litellm"}
_PROVIDERS = _RESPONSES | _CHAT | {"bedrock", "anthropic", "google", "ollama"}


def provider_of(candidate: ModelCandidate) -> str:
    assert candidate.id is not None
    return candidate.id.partition("/")[0] if "/" in candidate.id else "bedrock"


def validate_generation(spec: ModelSpec, defaults: ModelDefaults, options: GenerationOptions) -> None:
    if not options.generation_overrides():
        return
    for candidate in spec.candidates(default_id=defaults.default_id):
        if options.response_format is not None:
            params = (candidate.config or {}).get("params") or {}
            formats = (params.get("response_format"), (params.get("text") or {}).get("format"))
            if any(isinstance(fmt, dict) and fmt.get("type") == "json_schema" for fmt in formats):
                raise InvalidRequestError(
                    "response_format cannot override a JSON schema configured in model.config.params"
                )
        if candidate.module is not None:
            raise InvalidRequestError("generation overrides require model.id, not model.module (including fallbacks)")
        provider = provider_of(candidate)
        if provider not in _PROVIDERS:
            raise InvalidRequestError(f"generation overrides are not supported for provider {provider!r}")
        if options.reasoning_effort is not None and provider == "ollama":
            raise InvalidRequestError("reasoning_effort is not supported for provider 'ollama'")
        if (
            options.response_format is not None or options.tool_choice is not None
        ) and provider not in _RESPONSES | _CHAT:
            raise InvalidRequestError(f"response_format / tool_choice are not supported for provider {provider!r}")


def apply_generation(model: Model, provider: str, options: GenerationOptions, resolved_config: dict[str, Any]) -> None:
    """定義の適用後に指定項目だけを上書きする。辞書はコピーし、他の項目や共有の定義を変えない。"""
    values = options.generation_overrides()
    if not values:
        return
    config = deepcopy(model.get_config())
    updates: dict[str, Any] = {}
    params = dict(config.get("params") or {})
    if provider in _RESPONSES | _CHAT | {"google", "anthropic"}:
        for key in ("temperature", "top_p"):
            if key in values:
                params[key] = values[key]
    else:
        for key in ("temperature", "top_p"):
            if key in values:
                updates[key] = values[key]
    if options.max_output_tokens is not None:
        if provider in _RESPONSES | _CHAT | {"google"}:
            key = "max_completion_tokens" if provider in _CHAT else "max_output_tokens"
            params.pop("max_tokens", None)
            params[key] = options.max_output_tokens
        else:
            updates["max_tokens"] = options.max_output_tokens
            # Anthropic の params はトップレベルの max_tokens より後に展開される。
            params.pop("max_tokens", None)
    if options.reasoning_effort is not None:
        if provider in _CHAT:
            params.pop("reasoning_effort", None)
            if options.reasoning_effort != "auto":
                params["reasoning_effort"] = "none" if options.reasoning_effort == "off" else options.reasoning_effort
        elif provider in _RESPONSES:
            reasoning = dict(params.get("reasoning") or {})
            reasoning.update(resolved_config.get("params", {}).get("reasoning", {}))
            params["reasoning"] = reasoning
        elif provider == "google":
            thinking_config = dict(params.get("thinking_config") or {})
            for key in ("thinking_level", "thinking_budget"):
                thinking_config.pop(key, None)
            thinking_config.update(resolved_config.get("params", {}).get("thinking_config", {"thinking_budget": 0}))
            params["thinking_config"] = thinking_config
        elif provider in {"bedrock", "anthropic"}:
            container = "additional_request_fields" if provider == "bedrock" else "params"
            thinking = dict(config.get(container) or {})
            for key in ("thinking", "reasoningConfig", "reasoning", "reasoning_effort"):
                thinking.pop(key, None)
            output_config = dict(thinking.get("output_config") or {})
            output_config.pop("effort", None)
            thinking.pop("output_config", None)
            generated = deepcopy(resolved_config.get(container) or {})
            output_config.update(generated.pop("output_config", {}))
            thinking.update(generated)
            if output_config:
                thinking["output_config"] = output_config
            if provider == "bedrock":
                updates[container] = thinking
            else:
                # temperature / top_p / max_tokens のリクエスト指定も維持する。
                for key in ("temperature", "top_p"):
                    if key in values:
                        thinking[key] = values[key]
                if options.max_output_tokens is not None:
                    thinking.pop("max_tokens", None)
                params = thinking
    if options.response_format is not None:
        fmt = options.response_format.model_dump(exclude_none=True, by_alias=True)
        if provider in _RESPONSES:
            if fmt["type"] == "json_schema":
                fmt = {"type": "json_schema", **fmt["json_schema"]}
            params["text"] = {**(params.get("text") or {}), "format": fmt}
        else:
            params["response_format"] = fmt
    if options.tool_choice is not None:
        choice = options.tool_choice
        if isinstance(choice, FunctionToolChoice):
            params["tool_choice"] = (
                {"type": "function", "name": choice.function.name} if provider in _RESPONSES else choice.model_dump()
            )
        else:
            params["tool_choice"] = choice
    if provider in _RESPONSES | _CHAT | {"google", "anthropic"}:
        updates["params"] = params
    model.update_config(**updates)


def configure_tool_choice(agent: Agent, model: Model | ModelRouter, options: GenerationOptions) -> None:
    """強制指定は最初の成功したモデル呼び出しまで。以後は回答できるよう auto に戻す。"""
    choice = options.tool_choice
    if choice is None or choice == "auto":
        return
    models = [c.model for c in model.candidates] if isinstance(model, ModelRouter) else [model]
    released = False

    def check_tools(event: BeforeModelCallEvent) -> None:
        if released:
            return
        if choice == "required" and not event.agent.tool_names:
            raise InvalidRequestError("tool_choice='required' needs at least one registered tool")
        if isinstance(choice, FunctionToolChoice) and choice.function.name not in event.agent.tool_names:
            raise InvalidRequestError(f"unknown tool in tool_choice: {choice.function.name!r}")

    def release(event: AfterModelCallEvent) -> None:
        nonlocal released
        if released or event.stop_response is None or event.retry or choice == "none":
            return
        for candidate in models:
            assert isinstance(candidate, Model)
            candidate.update_config(params={**(candidate.get_config().get("params") or {}), "tool_choice": "auto"})
        released = True

    agent.add_hook(check_tools)
    # 優先度は After フックでも昇順。利用者フックによる再試行の判断を待ってから解除する。
    agent.add_hook(release, order=HookOrder.SDK_LAST)

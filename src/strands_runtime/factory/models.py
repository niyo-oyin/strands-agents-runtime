"""``[model]`` テーブルを ``strands.models.Model``（``fallbacks`` があれば ``ModelRouter``）にする。"""

from __future__ import annotations

import logging
import os
from copy import deepcopy
from typing import Any

from strands.models import CacheConfig, Model, ModelRouter
from strands_harness.models import resolve_model

from strands_runtime.definitions.schema import ModelCandidate, ModelDefaults, ModelSpec
from strands_runtime.errors import DefinitionError, InvalidRequestError
from strands_runtime.factory.references import load_attribute
from strands_runtime.factory.request_options import apply_generation, provider_of, validate_generation
from strands_runtime_client.inputs import GenerationOptions

logger = logging.getLogger(__name__)

OPENAI_CHAT_PROVIDER = "openai-chat"
"""ランタイム独自のプロバイダー。OpenAI 互換の Chat Completions エンドポイント（OpenRouter、vLLM、Ollama、LiteLLM など）向け。

ハーネスの ``openai/`` は Responses API を使い、互換サーバーの多くはそれを実装していないため、
こちらは ``strands.models.OpenAIModel`` を直接組み立てる。
接続先は ``base_url``（なければ openai SDK が読む ``OPENAI_BASE_URL``）、キーは ``api_key_env`` で名指しした環境変数
（なければ SDK の ``OPENAI_API_KEY``）。エージェントごとに両方を指定できるので、公式 OpenAI と互換サーバーを
1 つのランタイムで同時に使える。
"""


def override_model(spec: ModelSpec, defaults: ModelDefaults, model_id: str) -> ModelSpec:
    """接続設定を保ち、今回の主モデルだけを選ぶ。定義やフォールバック候補は変更しない。"""
    if spec.module is not None:
        raise InvalidRequestError("model cannot override a model.module definition; use a model.id definition")
    configured = spec.id or defaults.default_id
    provider, separator, name = model_id.partition("/")
    if not separator or not name or provider != configured.partition("/")[0]:
        raise InvalidRequestError(f"model must use the same provider as {configured!r}")
    # config.model_id が、リクエストで選んだ ID を再度上書きしないようにする。
    config = {key: value for key, value in spec.config.items() if key != "model_id"}
    return spec.model_copy(update={"id": model_id, "fallbacks": [], "config": config})


def build_model(
    spec: ModelSpec, defaults: ModelDefaults, options: GenerationOptions | None = None
) -> Model | ModelRouter:
    """1 エージェント分のモデルを組み立てる。

    主モデルと ``fallbacks`` の各候補は同じ手順で作る。
    ``provider/id`` 文字列は ``strands_harness.models.resolve_model``（effort とキャッシュをプロバイダー別に変換）、
    ``openai-chat/`` はランタイム独自の ``OpenAIModel`` 構築、``module`` 参照はそのまま使う。
    その後 ``[model.cache]`` と ``config`` を全候補に適用し、候補が複数なら失敗時に切り替える ``ModelRouter`` に包む。
    """
    try:
        candidates = spec.candidates(default_id=defaults.default_id)
    except ValueError as exc:
        raise DefinitionError(str(exc)) from exc
    if options is not None:
        validate_generation(spec, defaults, options)
    cache = CacheConfig(**spec.cache.model_dump()) if spec.cache is not None else None
    built = [_build_candidate(c, spec, defaults, cache, options) for c in candidates]

    if len(built) == 1:
        return built[0]
    # ``ModelRouter`` は入れ子にできるが、設定ファイルからの意図が曖昧になるので組み合わせは拒否する。
    for item in built:
        if isinstance(item, ModelRouter):
            raise DefinitionError("model.fallbacks cannot be combined with a module that already returns a ModelRouter")
    return ModelRouter(built)


def _build_candidate(
    candidate: ModelCandidate,
    spec: ModelSpec,
    defaults: ModelDefaults,
    cache: CacheConfig | None,
    options: GenerationOptions | None = None,
) -> Model | ModelRouter:
    """候補 1 つを ``Model`` にする。``module`` 参照、``openai-chat/``、それ以外（ハーネス解決）の 3 経路。"""
    effort = candidate.effort or "auto"
    # Chat 互換 API の effort は下流 API に渡す。ほかはハーネスのプロバイダー別変換を使う。
    if (
        options is not None
        and options.reasoning_effort is not None
        and candidate.id is not None
        and provider_of(candidate) not in {"openai-chat", "litellm"}
    ):
        effort = "off" if options.reasoning_effort == "none" else options.reasoning_effort
    model: Model | ModelRouter
    if candidate.module is not None:
        obj = load_attribute(candidate.module)
        # 参照先はインスタンスでも、引数なしのファクトリ関数でもよい。
        produced = obj() if callable(obj) and not isinstance(obj, Model | ModelRouter) else obj
        if not isinstance(produced, Model | ModelRouter):
            raise DefinitionError(f"model module {candidate.module!r} did not produce a Model or ModelRouter")
        model = produced
        if effort != "auto":
            logger.warning("effort=%r is ignored for a model module; configure reasoning in the module", effort)
    else:
        model_id = candidate.id
        assert model_id is not None
        if model_id in ("openai/", f"{OPENAI_CHAT_PROVIDER}/"):
            raise DefinitionError(f"model id {model_id!r} needs a model name after the slash")
        if model_id.startswith(f"{OPENAI_CHAT_PROVIDER}/"):
            model = _openai_chat(
                model_id.removeprefix(f"{OPENAI_CHAT_PROVIDER}/"),
                effort=effort,
                base_url=candidate.base_url,
                api_key_env=candidate.api_key_env,
            )
        else:
            model = resolve_model(
                model_id,
                default=defaults.default_id,
                effort=effort,
                caching=spec.caching,
                # ``caching`` を明示した場合は、キャッシュ非対応のプロバイダーで警告ではなくエラーにしてもらう。
                caching_explicit="caching" in spec.model_fields_set,
            )
            if model_id.startswith("openai/"):
                from strands.models.openai_responses import OpenAIResponsesModel

                # ハーネスの effort 変換を保ち、SDK が公開しているクライアント引数に接続設定を渡す。
                assert isinstance(model, OpenAIResponsesModel)
                model.client_args.update(_openai_client_args(candidate.base_url, candidate.api_key_env))

    if isinstance(model, ModelRouter):
        # ルーターには設定を適用する先（個々のモデル）がないため、候補側で設定してもらう。
        if cache is not None or candidate.config:
            raise DefinitionError(
                "model.cache / model.config cannot be applied to a ModelRouter; configure its candidates"
            )
        return model
    resolved_config = deepcopy(model.get_config())
    # ``update_config`` は後勝ちなので、ハーネスが決めた値（max_tokens など）を ``config`` で上書きできる。
    if cache is not None:
        model.update_config(cache_config=cache)
    if candidate.config:
        model.update_config(**candidate.config)
    if options is not None and options.generation_overrides():
        apply_generation(model, provider_of(candidate), options, resolved_config)
    return model


def _openai_chat(model_name: str, *, effort: str, base_url: str | None, api_key_env: str | None) -> Model:
    """OpenAI 互換 Chat Completions の ``OpenAIModel`` を作る。"""
    from strands.models.openai import OpenAIModel  # extras の [openai] が必要

    model = OpenAIModel(client_args=_openai_client_args(base_url, api_key_env) or None, model_id=model_name)
    if effort not in ("auto", "off"):
        # Chat Completions では推論の強さを ``reasoning_effort`` で指定する。
        # 対応していない互換サーバーはリクエストごと拒否するので、定義で明示されたときだけ送る。
        model.update_config(params={"reasoning_effort": effort})
    return model


def _openai_client_args(base_url: str | None, api_key_env: str | None) -> dict[str, Any]:
    """エージェント固有の接続設定。未指定の値は OpenAI SDK の環境変数解決に任せる。"""
    client_args: dict[str, Any] = {}
    if base_url:
        client_args["base_url"] = base_url
    if api_key_env:
        try:
            client_args["api_key"] = os.environ[api_key_env]
        except KeyError as exc:
            raise DefinitionError(f"environment variable {api_key_env!r} (api_key_env) is not set") from exc
    return client_args


def model_label(model: Model | ModelRouter) -> str:
    """メトリクス表示用の短い識別子。モデル ID か、ルーターなら既定モデルの ID。"""
    if isinstance(model, ModelRouter):
        return f"router({model_label(model.default_model)})"
    config = model.get_config()
    model_id = config.get("model_id") if isinstance(config, dict) else None
    return str(model_id) if model_id else type(model).__name__

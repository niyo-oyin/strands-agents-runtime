"""Turn a ``[model]`` table into a ``strands.models.Model`` (or a ``ModelRouter`` when fallbacks are declared)."""

from __future__ import annotations

import logging
import os
from typing import Any

from strands.models import CacheConfig, Model, ModelRouter
from strands_harness.models import resolve_model

from strands_runtime.definitions.schema import ModelCandidate, ModelDefaults, ModelSpec
from strands_runtime.errors import DefinitionError
from strands_runtime.factory.references import load_attribute

logger = logging.getLogger(__name__)

OPENAI_CHAT_PROVIDER = "openai-chat"
"""Runtime-native provider: OpenAI-compatible *Chat Completions* endpoints (OpenRouter, vLLM, Ollama, LiteLLM, ...).

The harness's ``openai/`` provider targets the Responses API, which most compatible servers do not
implement, so this one builds ``strands.models.OpenAIModel`` directly. The endpoint comes from
``base_url`` or the openai SDK's ``OPENAI_BASE_URL``; the key from the environment variable named by
``api_key_env`` (default: the SDK's own ``OPENAI_API_KEY``). Per-agent ``base_url`` + ``api_key_env`` is what
lets one runtime serve agents on OpenAI proper and on a compatible server at the same time.
"""

DEFAULT_SENTINEL = "__default__"


def build_model(spec: ModelSpec, defaults: ModelDefaults) -> Model | ModelRouter:
    """Build the model for one agent.

    Each candidate (the primary and its ``fallbacks``) is built the same way: ``provider/id`` strings go
    through ``strands_harness.models.resolve_model`` (effort and caching translated per provider), the
    runtime's own ``openai-chat/`` provider builds ``OpenAIModel``, and ``module`` references are used as-is.
    ``[model.cache]`` and ``config`` are applied to every candidate afterwards. With fallbacks the result
    is a ``ModelRouter`` that moves to the next candidate when a call fails.
    """
    candidates = spec.candidates()
    cache = CacheConfig(**spec.cache.model_dump()) if spec.cache is not None else None
    built = [_build_candidate(c, spec, defaults, cache) for c in candidates]

    if len(built) == 1:
        return built[0]
    for item in built:
        if isinstance(item, ModelRouter):
            raise DefinitionError("model.fallbacks cannot be combined with a module that already returns a ModelRouter")
    return ModelRouter(built)


def _build_candidate(
    candidate: ModelCandidate, spec: ModelSpec, defaults: ModelDefaults, cache: CacheConfig | None
) -> Model | ModelRouter:
    effort = candidate.effort or "auto"
    model: Model | ModelRouter
    if candidate.module is not None:
        obj = load_attribute(candidate.module)
        produced = obj() if callable(obj) and not isinstance(obj, Model | ModelRouter) else obj
        if not isinstance(produced, Model | ModelRouter):
            raise DefinitionError(f"model module {candidate.module!r} did not produce a Model or ModelRouter")
        model = produced
        if effort != "auto":
            logger.warning("effort=%r is ignored for a model module; configure reasoning in the module", effort)
    else:
        model_id = defaults.default_id if candidate.id in (None, DEFAULT_SENTINEL) else candidate.id
        assert model_id is not None
        if model_id.startswith(f"{OPENAI_CHAT_PROVIDER}/"):
            model = _openai_chat(
                model_id.removeprefix(f"{OPENAI_CHAT_PROVIDER}/"),
                effort=effort,
                base_url=candidate.base_url,
                api_key_env=candidate.api_key_env,
            )
        else:
            if candidate.base_url or candidate.api_key_env:
                raise DefinitionError(f"base_url / api_key_env need an '{OPENAI_CHAT_PROVIDER}/' id, got {model_id!r}")
            model = resolve_model(
                model_id,
                default=defaults.default_id,
                effort=effort,
                caching=spec.caching,
                caching_explicit="caching" in spec.model_fields_set,
            )

    if isinstance(model, ModelRouter):
        if cache is not None or candidate.config:
            raise DefinitionError(
                "model.cache / model.config cannot be applied to a ModelRouter; configure its candidates"
            )
        return model
    if cache is not None:
        model.update_config(cache_config=cache)
    if candidate.config:
        model.update_config(**candidate.config)
    return model


def _openai_chat(model_name: str, *, effort: str, base_url: str | None, api_key_env: str | None) -> Model:
    from strands.models.openai import OpenAIModel  # optional extra: [openai]

    if not model_name:
        raise DefinitionError(f"model id '{OPENAI_CHAT_PROVIDER}/' needs a model name after the slash")
    client_args: dict[str, Any] = {}
    if base_url:
        client_args["base_url"] = base_url
    if api_key_env:
        try:
            client_args["api_key"] = os.environ[api_key_env]
        except KeyError as exc:
            raise DefinitionError(f"environment variable {api_key_env!r} (api_key_env) is not set") from exc
    model = OpenAIModel(client_args=client_args or None, model_id=model_name)
    if effort not in ("auto", "off"):
        # Chat Completions spelling for reasoning models; compatible servers that lack it reject the request,
        # so it is only sent when the definition asks for a level explicitly.
        model.update_config(params={"reasoning_effort": effort})
    return model


def model_label(model: Model | ModelRouter) -> str:
    """A short human-readable identity for metrics: the model id, or the router's default model id."""
    if isinstance(model, ModelRouter):
        return f"router({model_label(model.default_model)})"
    config = model.get_config()
    model_id = config.get("model_id") if isinstance(config, dict) else None
    return str(model_id) if model_id else type(model).__name__

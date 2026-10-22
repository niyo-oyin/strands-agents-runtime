"""Turn a ``[model]`` table into a ``strands.models.Model`` (or ``ModelRouter``)."""

from __future__ import annotations

import logging
import os
from typing import Any

from strands.models import Model, ModelRouter
from strands_harness.models import resolve_model

from strands_runtime.definitions.schema import ModelSpec
from strands_runtime.errors import DefinitionError
from strands_runtime.factory.references import load_attribute

logger = logging.getLogger(__name__)


OPENAI_CHAT_PROVIDER = "openai-chat"
"""Runtime-native provider: OpenAI-compatible *Chat Completions* endpoints (vLLM, Ollama, LiteLLM proxy, ...).

The harness's ``openai/`` provider targets the Responses API, which most compatible servers do not
implement, so this one builds ``strands.models.OpenAIModel`` directly. The endpoint comes from
``model.base_url`` or ``OPENAI_BASE_URL``; the key from the variable named by ``model.api_key_env``
(default: the openai SDK's own ``OPENAI_API_KEY``). Per-agent ``base_url`` + ``api_key_env`` is what lets
one runtime serve agents on OpenAI proper and on a compatible server at the same time.
"""


def build_model(spec: ModelSpec, *, default_id: str) -> Model | ModelRouter:
    """Build the model for one agent.

    ``provider/id`` strings go through ``strands_harness.models.resolve_model`` so effort and prompt
    caching are translated to each provider's request fields, except the runtime's own
    ``openai-chat/`` provider. ``module`` references are used as-is. ``config`` is applied last via
    ``Model.update_config`` and may therefore override anything the resolver chose (for example ``max_tokens``).
    """
    model: Model | ModelRouter
    model_id = spec.id or default_id
    if spec.module is not None:
        obj = load_attribute(spec.module)
        produced = obj() if callable(obj) and not isinstance(obj, Model | ModelRouter) else obj
        if not isinstance(produced, Model | ModelRouter):
            raise DefinitionError(f"model.module {spec.module!r} did not produce a Model or ModelRouter")
        model = produced
        if spec.effort != "auto":
            logger.warning(
                "model.effort=%r is ignored for model.module; configure reasoning in the module", spec.effort
            )
    elif model_id.startswith(f"{OPENAI_CHAT_PROVIDER}/"):
        model = _openai_chat(model_id.removeprefix(f"{OPENAI_CHAT_PROVIDER}/"), spec)
    else:
        model = resolve_model(
            spec.id,
            default=default_id,
            effort=spec.effort,
            caching=spec.caching,
            caching_explicit="caching" in spec.model_fields_set,
        )

    if spec.config:
        if isinstance(model, ModelRouter):
            raise DefinitionError("model.config cannot be applied to a ModelRouter; configure the candidates instead")
        model.update_config(**spec.config)
    return model


def _openai_chat(model_name: str, spec: ModelSpec) -> Model:
    from strands.models.openai import OpenAIModel  # optional extra: [openai]

    if not model_name:
        raise DefinitionError("model.id 'openai-chat/' needs a model name after the slash")
    params: dict[str, Any] = {}
    if spec.effort not in ("auto", "off"):
        # Chat Completions spelling for reasoning models; compatible servers that lack it reject the request,
        # so it is only sent when the definition asks for a level explicitly.
        params["reasoning_effort"] = spec.effort
    client_args: dict[str, Any] = {}
    if spec.base_url:
        client_args["base_url"] = spec.base_url
    if spec.api_key_env:
        try:
            client_args["api_key"] = os.environ[spec.api_key_env]
        except KeyError as exc:
            raise DefinitionError(f"environment variable {spec.api_key_env!r} (model.api_key_env) is not set") from exc
    model = OpenAIModel(client_args=client_args or None, model_id=model_name)
    if params:
        model.update_config(params=params)
    return model

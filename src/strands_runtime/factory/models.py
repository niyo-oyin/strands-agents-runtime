"""Turn a ``[model]`` table into a ``strands.models.Model`` (or ``ModelRouter``)."""

from __future__ import annotations

import logging

from strands.models import Model, ModelRouter
from strands_harness.models import resolve_model

from strands_runtime.definitions.schema import ModelSpec
from strands_runtime.errors import DefinitionError
from strands_runtime.factory.references import load_attribute

logger = logging.getLogger(__name__)


def build_model(spec: ModelSpec, *, default_id: str) -> Model | ModelRouter:
    """Build the model for one agent.

    ``provider/id`` strings go through ``strands_harness.models.resolve_model`` so effort and prompt
    caching are translated to each provider's request fields. ``module`` references are used as-is.
    ``config`` is applied last via ``Model.update_config`` and may therefore override anything the
    resolver chose (for example ``max_tokens``).
    """
    model: Model | ModelRouter
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

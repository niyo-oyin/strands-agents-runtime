"""Resolve ``pkg.module:attr`` / ``/abs/path.py:attr`` references from definitions to Python objects."""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import sys
from pathlib import Path
from typing import Any, TypeVar

from strands_runtime.definitions.loader import is_path_reference
from strands_runtime.errors import ReferenceResolutionError

T = TypeVar("T")


def _import_file(path: Path) -> Any:
    module_name = f"_strands_runtime_ref_{abs(hash(str(path)))}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ReferenceResolutionError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        del sys.modules[module_name]
        raise ReferenceResolutionError(f"error importing {path}: {exc}") from exc
    return module


def load_attribute(reference: str) -> Any:
    """Return the object named by ``module:attr`` (module path or absolute ``.py`` path)."""
    module_part, sep, attr = reference.partition(":")
    if not sep or not attr:
        raise ReferenceResolutionError(f"{reference!r} must be of the form 'module:attribute'")
    if is_path_reference(module_part):
        module = _import_file(Path(module_part))
    else:
        try:
            module = importlib.import_module(module_part)
        except ImportError as exc:
            raise ReferenceResolutionError(f"cannot import module {module_part!r}: {exc}") from exc
    try:
        return getattr(module, attr)
    except AttributeError as exc:
        raise ReferenceResolutionError(f"{module_part!r} has no attribute {attr!r}") from exc


def resolve_instance[T](reference: str, expected: type[T]) -> T:
    """Resolve a reference to an *instance* of ``expected``.

    The attribute may be an instance already, a class (instantiated with no arguments), or a
    zero-argument factory function. This keeps definitions terse while still allowing code to
    configure complex objects.
    """
    obj = load_attribute(reference)
    if isinstance(obj, expected):
        return obj
    if inspect.isclass(obj) or callable(obj):
        try:
            produced = obj()
        except TypeError as exc:
            raise ReferenceResolutionError(f"{reference!r} could not be called without arguments: {exc}") from exc
        if isinstance(produced, expected):
            return produced
        raise ReferenceResolutionError(
            f"{reference!r} returned {type(produced).__name__}, expected {expected.__name__}"
        )
    raise ReferenceResolutionError(f"{reference!r} is a {type(obj).__name__}, expected {expected.__name__}")


def resolve_class[T](reference: str, expected: type[T]) -> type[T]:
    """Resolve a reference to a subclass of ``expected`` (used for pydantic structured-output models)."""
    obj = load_attribute(reference)
    if inspect.isclass(obj) and issubclass(obj, expected):
        return obj
    raise ReferenceResolutionError(f"{reference!r} is not a subclass of {expected.__name__}")

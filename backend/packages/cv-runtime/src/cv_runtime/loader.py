from __future__ import annotations

import importlib
from functools import lru_cache

from cv_runtime.errors import RuntimeHandlerLoadError
from cv_runtime.handlers import RuntimeNodeHandler, assert_handler_compatible


@lru_cache(maxsize=512)
def load_runtime_handler(
    runtime_handler_ref: str,
    *,
    expected_node_type: str,
) -> RuntimeNodeHandler:
    module_name, separator, attribute_name = runtime_handler_ref.partition(":")
    if not module_name or separator != ":" or not attribute_name:
        raise RuntimeHandlerLoadError(
            f"Invalid runtime_handler_ref {runtime_handler_ref!r}; expected 'module:attribute'."
        )
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        raise RuntimeHandlerLoadError(
            f"Could not import runtime handler module {module_name!r}: {exc}"
        ) from exc
    try:
        attribute = getattr(module, attribute_name)
    except AttributeError as exc:
        raise RuntimeHandlerLoadError(
            f"Runtime handler module {module_name!r} has no attribute {attribute_name!r}."
        ) from exc
    handler = attribute() if isinstance(attribute, type) else attribute
    try:
        return assert_handler_compatible(handler, expected_node_type=expected_node_type)
    except TypeError as exc:
        raise RuntimeHandlerLoadError(str(exc)) from exc


def validate_runtime_handler_ref(runtime_handler_ref: str, *, expected_node_type: str) -> None:
    load_runtime_handler(runtime_handler_ref, expected_node_type=expected_node_type)

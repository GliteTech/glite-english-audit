"""Source adapter packages and their registration.

Each adapter package exposes ``ADAPTER_ID`` and ``create_adapter()``.
Registration is explicit: callers invoke :func:`register_all` once; importing
this package alone registers nothing, so tests keep full registry control.
"""

import importlib
from collections.abc import Callable
from typing import cast

from glite_english_audit.discovery.base import SourceAdapter
from glite_english_audit.discovery.registry import adapter_ids, register_adapter

_ADAPTER_MODULES = (
    "glite_english_audit.adapters.claude_code",
    "glite_english_audit.adapters.codex",
)


def register_all() -> None:
    """Register every implemented adapter, once. Idempotent."""
    registered = set(adapter_ids())
    for module_name in _ADAPTER_MODULES:
        module = importlib.import_module(module_name)
        adapter_id = cast(str, module.ADAPTER_ID)
        factory = cast(Callable[[], SourceAdapter], module.create_adapter)
        if adapter_id not in registered:
            register_adapter(adapter_id, factory)

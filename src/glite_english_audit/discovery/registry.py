"""Registry of implemented source adapters.

Adapters register here as they are implemented. The stable adapter IDs and
their target maturity for V1 come from specification 1.4; an ID appears in
this registry only once its adapter exists and its source specification under
``specifications/sources/`` has been reviewed.
"""

from collections.abc import Callable

from glite_english_audit.discovery.base import SourceAdapter

_FACTORIES: dict[str, Callable[[], SourceAdapter]] = {}


def register_adapter(adapter_id: str, factory: Callable[[], SourceAdapter]) -> None:
    """Register an adapter factory under its stable public ID."""
    if adapter_id in _FACTORIES:
        msg = f"adapter already registered: {adapter_id!r}"
        raise ValueError(msg)
    _FACTORIES[adapter_id] = factory


def adapter_ids() -> list[str]:
    """Registered adapter IDs, sorted for deterministic iteration."""
    return sorted(_FACTORIES)


def create_adapter(adapter_id: str) -> SourceAdapter:
    """Instantiate the adapter registered under ``adapter_id``."""
    try:
        factory = _FACTORIES[adapter_id]
    except KeyError as exc:
        msg = f"no adapter registered for {adapter_id!r}"
        raise KeyError(msg) from exc
    return factory()


def create_all_adapters() -> list[SourceAdapter]:
    """Instantiate every registered adapter, in deterministic order."""
    return [create_adapter(adapter_id) for adapter_id in adapter_ids()]

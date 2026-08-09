"""Aider source adapter (beta).

Source specification: ``specifications/sources/aider.md``.
"""

from glite_english_audit.adapters.aider.adapter import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    PRODUCER_VERSION,
    AiderAdapter,
    create_adapter,
)

__all__ = [
    "ADAPTER_ID",
    "ADAPTER_VERSION",
    "PRODUCER_VERSION",
    "AiderAdapter",
    "create_adapter",
]

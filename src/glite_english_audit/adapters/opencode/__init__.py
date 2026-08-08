"""OpenCode source adapter (stable).

Source specification: ``specifications/sources/opencode.md``.
"""

from glite_english_audit.adapters.opencode.adapter import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    PRODUCER_VERSION,
    OpenCodeAdapter,
    create_adapter,
)

__all__ = [
    "ADAPTER_ID",
    "ADAPTER_VERSION",
    "PRODUCER_VERSION",
    "OpenCodeAdapter",
    "create_adapter",
]

"""Cline source adapter (stable).

Source specification: ``specifications/sources/cline.md``.
"""

from glite_english_audit.adapters.cline.adapter import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    PRODUCER_VERSION,
    ClineAdapter,
    create_adapter,
)

__all__ = [
    "ADAPTER_ID",
    "ADAPTER_VERSION",
    "PRODUCER_VERSION",
    "ClineAdapter",
    "create_adapter",
]

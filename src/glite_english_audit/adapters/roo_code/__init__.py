"""Roo Code source adapter (stable).

Source specification: ``specifications/sources/roo_code.md``.
"""

from glite_english_audit.adapters.roo_code.adapter import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    PRODUCER_VERSION,
    RooCodeAdapter,
    create_adapter,
)

__all__ = [
    "ADAPTER_ID",
    "ADAPTER_VERSION",
    "PRODUCER_VERSION",
    "RooCodeAdapter",
    "create_adapter",
]

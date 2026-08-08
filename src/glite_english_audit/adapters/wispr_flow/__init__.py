"""Wispr Flow dictation source adapter (beta, strict fingerprint gate).

Source specification: ``specifications/sources/wispr_flow.md``.
"""

from glite_english_audit.adapters.wispr_flow.adapter import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    ALLOWED_SOURCE_NAMES,
    NEVER_OPEN_DIR_NAMES,
    NEVER_OPEN_NAMES,
    PRODUCER_VERSION,
    WisprFlowAdapter,
    create_adapter,
)

__all__ = [
    "ADAPTER_ID",
    "ADAPTER_VERSION",
    "ALLOWED_SOURCE_NAMES",
    "NEVER_OPEN_DIR_NAMES",
    "NEVER_OPEN_NAMES",
    "PRODUCER_VERSION",
    "WisprFlowAdapter",
    "create_adapter",
]

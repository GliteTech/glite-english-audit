"""Cursor IDE source adapter (beta, inventory only).

Source specification: ``specifications/sources/cursor.md``. Rawness is
unknown (native Voice Mode), so the adapter inventories chat data and
contributes no analyzable text (project specification 4.7).
"""

from glite_english_audit.adapters.cursor.adapter import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    BETA_NO_TEXT_REASON,
    PRODUCER_VERSION,
    CursorAdapter,
    create_adapter,
)

__all__ = [
    "ADAPTER_ID",
    "ADAPTER_VERSION",
    "BETA_NO_TEXT_REASON",
    "PRODUCER_VERSION",
    "CursorAdapter",
    "create_adapter",
]

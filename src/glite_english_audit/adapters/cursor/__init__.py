"""Cursor IDE source adapter.

Source specification: ``specifications/sources/cursor.md``. Evidence E11 proved
the tested macOS G4 variant stores prompts verbatim, so each user bubble is
reconciled against its Lexical editor state: reconciled bubbles contribute
text, everything else is inventoried only (project specification 4.7).
"""

from glite_english_audit.adapters.cursor.adapter import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    AUTHORSHIP_BASIS,
    PRODUCER_VERSION,
    PROJECTION_DRIFT_REASON,
    RECONCILED_TEXT_REASON,
    UNPROVEN_VARIANT_REASON,
    CursorAdapter,
    create_adapter,
)
from glite_english_audit.adapters.cursor.lexical import (
    GateResult,
    LexicalProjection,
    TextGate,
    project_editor_state,
    reconcile,
    strip_mentions,
)

__all__ = [
    "ADAPTER_ID",
    "ADAPTER_VERSION",
    "AUTHORSHIP_BASIS",
    "PRODUCER_VERSION",
    "PROJECTION_DRIFT_REASON",
    "RECONCILED_TEXT_REASON",
    "UNPROVEN_VARIANT_REASON",
    "CursorAdapter",
    "GateResult",
    "LexicalProjection",
    "TextGate",
    "create_adapter",
    "project_editor_state",
    "reconcile",
    "strip_mentions",
]

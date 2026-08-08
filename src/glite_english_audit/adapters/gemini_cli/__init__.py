"""Google Gemini CLI source adapter (stable).

Source specification: ``specifications/sources/gemini_cli.md``.
"""

from glite_english_audit.adapters.gemini_cli.adapter import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    PRODUCER_VERSION,
    GeminiCliAdapter,
    create_adapter,
)

__all__ = [
    "ADAPTER_ID",
    "ADAPTER_VERSION",
    "PRODUCER_VERSION",
    "GeminiCliAdapter",
    "create_adapter",
]

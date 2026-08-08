"""Claude Code source adapter (stable).

Source specification: ``specifications/sources/claude_code.md``.
"""

from glite_english_audit.adapters.claude_code.adapter import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    PRODUCER_VERSION,
    ClaudeCodeAdapter,
    create_adapter,
)

__all__ = [
    "ADAPTER_ID",
    "ADAPTER_VERSION",
    "PRODUCER_VERSION",
    "ClaudeCodeAdapter",
    "create_adapter",
]

"""OpenAI Codex CLI source adapter package (stable adapter ID ``codex``)."""

from glite_english_audit.adapters.codex.adapter import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    NEVER_OPEN_NAMES,
    PRODUCER_VERSION,
    CodexAdapter,
    create_adapter,
)

__all__ = [
    "ADAPTER_ID",
    "ADAPTER_VERSION",
    "NEVER_OPEN_NAMES",
    "PRODUCER_VERSION",
    "CodexAdapter",
    "create_adapter",
]

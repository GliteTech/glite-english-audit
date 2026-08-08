"""Glite English Audit: local-first detection of non-native English mistakes.

The package holds the deterministic side of the audit: discovery, extraction,
normalization, verification, counting, state management, and packaging.
Semantic judgments are made by the active Codex or Claude Code runtime through
the skills in ``skills/``; no module in this package calls an inference API.
"""

CLIENT_VERSION = "0.1.0"

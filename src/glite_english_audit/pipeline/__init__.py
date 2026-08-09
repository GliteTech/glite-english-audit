"""Executable drivers for the audit waterfall.

Each step of the waterfall needs something an agent can actually run. The
skills describe judgment; these modules do the deterministic work between the
judgments: create a run, snapshot and extract selected sources, prepare
batches for the semantic steps, promote privacy-approved records, and
assemble the reviewed submission artifact.

Every module prints aggregate numbers only. No step driver returns source
text to the agent conversation.
"""

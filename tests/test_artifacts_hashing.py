"""Canonical JSON, SHA-256 vectors, and identifier generators."""

import re

from glite_english_audit.artifacts.hashing import (
    canonical_json_bytes,
    model_canonical_hash,
    new_artifact_id,
    new_recovery_secret,
    new_run_id,
    new_submission_id,
    sha256_hex,
)
from glite_english_audit.artifacts.models import EvidenceSpan

# Mirrors of the private patterns in artifacts/submission.py; a divergence is a
# contract change and must fail here.
_SUBMISSION_ID_PATTERN = re.compile(r"^sub-[0-9a-f]{32}$")
_RECOVERY_SECRET_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def test_canonical_json_sorts_keys() -> None:
    assert canonical_json_bytes({"b": 1, "a": 2}) == b'{"a":2,"b":1}'


def test_canonical_json_is_order_independent() -> None:
    first = canonical_json_bytes({"x": 1, "y": [1, 2], "z": {"b": 2, "a": 1}})
    second = canonical_json_bytes({"z": {"a": 1, "b": 2}, "y": [1, 2], "x": 1})
    assert first == second


def test_canonical_json_is_compact() -> None:
    assert b" " not in canonical_json_bytes({"a": [1, 2, 3], "b": {"c": 1}})


def test_canonical_json_keeps_unicode_unescaped() -> None:
    assert canonical_json_bytes({"note": "café"}) == '{"note":"café"}'.encode()


def test_sha256_known_vectors() -> None:
    assert sha256_hex(b"") == ("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
    assert sha256_hex(b"abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_model_canonical_hash_is_deterministic() -> None:
    first = EvidenceSpan(start=1, end=4)
    second = EvidenceSpan(start=1, end=4)
    assert model_canonical_hash(first) == model_canonical_hash(second)


def test_model_canonical_hash_respects_exclude() -> None:
    first = EvidenceSpan(start=1, end=4)
    second = EvidenceSpan(start=1, end=9)
    assert model_canonical_hash(first) != model_canonical_hash(second)
    assert model_canonical_hash(first, exclude={"end"}) == model_canonical_hash(
        second, exclude={"end"}
    )


def test_new_run_id_pattern_and_uniqueness() -> None:
    value = new_run_id()
    assert re.fullmatch(r"run-[0-9a-f]{32}", value)
    assert new_run_id() != value


def test_new_artifact_id_pattern() -> None:
    assert re.fullmatch(r"art-[0-9a-f]{32}", new_artifact_id())


def test_new_submission_id_matches_submission_contract_pattern() -> None:
    value = new_submission_id()
    assert _SUBMISSION_ID_PATTERN.fullmatch(value)
    assert new_submission_id() != value


def test_new_recovery_secret_matches_submission_contract_pattern() -> None:
    value = new_recovery_secret()
    assert _RECOVERY_SECRET_PATTERN.fullmatch(value)
    assert new_recovery_secret() != value

"""Envelope validation: timezone enforcement, hash checking, immutability."""

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from glite_english_audit.artifacts.enums import StepId
from glite_english_audit.artifacts.envelope import ArtifactEnvelope, utc_now

_HEX64 = "a" * 64


def _envelope(**overrides: Any) -> ArtifactEnvelope:
    data: dict[str, Any] = {
        "schema_name": "source_inventory",
        "schema_version": 1,
        "artifact_id": "art-" + "0" * 32,
        "run_id": "run-" + "0" * 32,
        "step_id": StepId.A_COLLECTED,
        "producer_name": "test-producer",
        "producer_version": "0.0.1",
        "created_at": datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC),
    }
    data.update(overrides)
    return ArtifactEnvelope(**data)


def test_utc_now_is_timezone_aware() -> None:
    now = utc_now()
    assert now.tzinfo is not None
    assert now.utcoffset() is not None


def test_envelope_accepts_timezone_aware_created_at() -> None:
    envelope = _envelope()
    assert envelope.created_at.tzinfo is not None


def test_envelope_rejects_naive_created_at() -> None:
    with pytest.raises(ValidationError):
        _envelope(created_at=datetime(2026, 8, 8, 12, 0, 0))


def test_envelope_accepts_valid_input_hashes() -> None:
    envelope = _envelope(input_hashes={"art-" + "1" * 32: _HEX64})
    assert envelope.input_hashes["art-" + "1" * 32] == _HEX64


@pytest.mark.parametrize("bad_digest", ["", "a" * 63, "a" * 65, "g" * 64, "A" * 64])
def test_envelope_rejects_malformed_input_hashes(bad_digest: str) -> None:
    with pytest.raises(ValidationError):
        _envelope(input_hashes={"art-" + "1" * 32: bad_digest})


def test_envelope_is_frozen() -> None:
    envelope = _envelope()
    with pytest.raises(ValidationError):
        envelope.producer_name = "another-producer"


def test_envelope_rejects_undeclared_fields() -> None:
    with pytest.raises(ValidationError):
        _envelope(source_path="/somewhere/private")

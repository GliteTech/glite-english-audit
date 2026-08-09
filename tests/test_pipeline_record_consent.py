"""All four consent moments must be recordable, not just the two with callers.

Specification 2.2 names four. Two had a field on ConsentState and no code path
that ever set it, and the fourth had no field at all: the review page held the
age attestation and the storage acceptance in memory and the server wrote
nothing to disk. A finished run's manifest therefore claimed the user had
never agreed to anything about sending.

That is the failure mode a consent record exists to prevent. Reading such a
manifest afterwards, nobody can tell a consent that was never asked for from
one that was given and never written down.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from glite_english_audit.artifacts.enums import AgentRuntime, OsEnvironment
from glite_english_audit.artifacts.manifest import CompatibilityFingerprint, ConsentState
from glite_english_audit.pipeline.record_consent import MOMENTS, missing_moments, record_consent
from glite_english_audit.state.run_store import create_run, load_manifest

_NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


@pytest.fixture
def run(tmp_path: Path) -> str:
    manifest = create_run(
        AgentRuntime.CLAUDE_CODE,
        OsEnvironment.MACOS,
        ConsentState(consent_policy_version="1"),
        CompatibilityFingerprint(
            adapter_versions={"claude_code": "1.0.0"},
            artifact_schema_version=1,
            tokenizer_version="1.0.0",
            skill_versions={"find-english-mistakes": 1},
            prompt_versions={"find-mistakes": 1},
            model_ids={"find-mistakes": "example-model-1"},
            consent_policy_version="1",
        ),
        root=tmp_path,
    )
    return manifest.run_id


def test_a_fresh_run_has_recorded_no_consent(run: str, tmp_path: Path) -> None:
    assert missing_moments(load_manifest(run, root=tmp_path), MOMENTS) == list(MOMENTS)


@pytest.mark.parametrize("moment", MOMENTS)
def test_every_named_moment_can_be_recorded(moment: str, run: str, tmp_path: Path) -> None:
    record_consent(run, moment, runs_root=tmp_path, now=_NOW)
    assert moment not in missing_moments(load_manifest(run, root=tmp_path), MOMENTS)


def test_recording_one_moment_records_only_that_moment(run: str, tmp_path: Path) -> None:
    # Provider-transfer consent is never inferred from anything, including
    # another consent given seconds earlier in the same conversation.
    record_consent(run, "local-scan", runs_root=tmp_path, now=_NOW)
    consent = load_manifest(run, root=tmp_path).consent
    assert consent.local_scan_confirmed_at == _NOW
    assert consent.provider_transfer_confirmed_at is None
    assert consent.preflight_confirmed_at is None


def test_the_two_send_confirmations_are_separate(run: str, tmp_path: Path) -> None:
    # The review page shows two unchecked boxes and either may be ticked
    # alone. One timestamp for both would record a single act the user never
    # performed.
    record_consent(run, "adult", runs_root=tmp_path, now=_NOW)
    consent = load_manifest(run, root=tmp_path).consent
    assert consent.adult_confirmed_at == _NOW
    assert consent.storage_terms_confirmed_at is None


def test_a_second_recording_keeps_the_original_time(run: str, tmp_path: Path) -> None:
    # A consent is evidence that a person agreed at a moment. Moving the
    # timestamp later would misdate the evidence.
    record_consent(run, "preflight", runs_root=tmp_path, now=_NOW)
    record_consent(run, "preflight", runs_root=tmp_path, now=_NOW + timedelta(hours=2))
    assert load_manifest(run, root=tmp_path).consent.preflight_confirmed_at == _NOW


def test_an_unknown_moment_is_refused(run: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown consent moment"):
        record_consent(run, "whatever-the-caller-felt-like", runs_root=tmp_path, now=_NOW)

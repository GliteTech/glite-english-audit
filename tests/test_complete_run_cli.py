"""The end of the pipeline, which for a long time did not exist.

`record_step` advances a run as far as `review` and stops. Nothing set
`completed`, and `cleanup_completed` -- which deletes the learner's sentences
the moment a run finishes -- refuses any run that is not finished, so it could
never run at all. Two promises broke together: the skill's "when the run
completes, immediately delete extracted source text", and resume, because a run
left in `review` is by definition unfinished and was offered again for ever.
"""

import json
from pathlib import Path

import pytest

from glite_english_audit.artifacts.enums import AgentRuntime, OsEnvironment, RunStatus
from glite_english_audit.artifacts.manifest import CompatibilityFingerprint, ConsentState
from glite_english_audit.pipeline.complete_run import complete_run, main
from glite_english_audit.state.machine import advance_run
from glite_english_audit.state.run_store import RunStoreError, create_run, save_manifest


def _fingerprint() -> CompatibilityFingerprint:
    return CompatibilityFingerprint.model_validate(
        {
            "adapter_versions": {"codex": "1.0.0"},
            "artifact_schema_version": 1,
            "tokenizer_version": "1.0.0",
            "skill_versions": {"find-english-mistakes": 1},
            "prompt_versions": {},
            "model_ids": {},
            "consent_policy_version": "1",
        }
    )


def _run_in_review(root: Path) -> str:
    manifest = create_run(
        AgentRuntime.CLAUDE_CODE,
        OsEnvironment.MACOS,
        ConsentState(consent_policy_version="1"),
        _fingerprint(),
        root=root,
    )
    for target in (
        RunStatus.SELECTING,
        RunStatus.AWAITING_PREFLIGHT,
        RunStatus.PROCESSING,
        RunStatus.REVIEW,
    ):
        manifest.status = advance_run(manifest.status, target)
    save_manifest(manifest, root=root)

    text = root / manifest.run_id / "steps" / "a-collected"
    text.mkdir(parents=True, exist_ok=True)
    (text / "session-0001.jsonl").write_text('{"text": "synthetic"}\n', encoding="utf-8")
    return manifest.run_id


def test_completing_a_run_deletes_the_learners_sentences_immediately(tmp_path: Path) -> None:
    """ "When the run completes, immediately" -- not in thirty days."""
    run_id = _run_in_review(tmp_path)
    text = tmp_path / run_id / "steps" / "a-collected" / "session-0001.jsonl"
    assert text.is_file()

    report = complete_run(run_id, root=tmp_path)

    assert report["status"] == "completed"
    assert not text.exists()
    assert not (tmp_path / run_id / "steps").exists()
    # The manifest survives: it is the record that the run existed and ended.
    assert (tmp_path / run_id / "run-manifest.json").is_file()


def test_a_completed_run_is_no_longer_offered_for_resume(tmp_path: Path) -> None:
    """A run left in `review` is unfinished, and was offered again for ever."""
    from glite_english_audit.state.run_store import list_unfinished

    run_id = _run_in_review(tmp_path)
    assert [summary.run_id for summary in list_unfinished(tmp_path)] == [run_id]

    complete_run(run_id, root=tmp_path)

    assert list_unfinished(tmp_path) == []


def test_withheld_records_are_recorded_as_a_different_ending(tmp_path: Path) -> None:
    run_id = _run_in_review(tmp_path)
    report = complete_run(run_id, outcome=RunStatus.COMPLETED_WITH_EXCLUSIONS, root=tmp_path)
    assert report["status"] == "completed_with_exclusions"


def test_running_it_twice_is_not_an_error(tmp_path: Path) -> None:
    """A crash between writing the status and deleting the text must be recoverable.

    The status is written first for that reason, so the second attempt finds a
    completed run with its text still there and finishes the job.
    """
    run_id = _run_in_review(tmp_path)
    complete_run(run_id, root=tmp_path)

    again = complete_run(run_id, root=tmp_path)

    assert again["already_completed"] is True
    assert again["private_text_removed"] is True


def test_an_unfinished_run_is_refused_rather_than_stripped(tmp_path: Path) -> None:
    """Only a completion deletes on the spot; anything else waits for retention."""
    manifest = create_run(
        AgentRuntime.CLAUDE_CODE,
        OsEnvironment.MACOS,
        ConsentState(consent_policy_version="1"),
        _fingerprint(),
        root=tmp_path,
    )
    manifest.status = advance_run(manifest.status, RunStatus.SELECTING)
    save_manifest(manifest, root=tmp_path)

    with pytest.raises((RunStoreError, ValueError)):
        complete_run(manifest.run_id, outcome=RunStatus.PROCESSING, root=tmp_path)


def test_the_cli_reports_what_it_did(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run_id = _run_in_review(tmp_path)
    assert main(["--run-id", run_id, "--runs-root", str(tmp_path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["private_text_removed"] is True


def test_the_cli_fails_loudly_on_an_unknown_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--run-id", "run-" + "f" * 32, "--runs-root", str(tmp_path)]) == 1

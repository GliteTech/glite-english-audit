"""The whole waterfall must run stage by stage through its own drivers.

This is the test the project lacked: every stage executed through the module
an agent would actually invoke, over committed fixtures, with a deterministic
stand-in for the semantic stages. It fails if any stage driver stops being
runnable, not merely if a function returns the wrong value.
"""

import json
from pathlib import Path

import pytest

from glite_english_audit.adapters.claude_code import create_adapter as claude_code_adapter
from glite_english_audit.artifacts.enums import ExampleType, Modality, StageId
from glite_english_audit.artifacts.envelope import ArtifactEnvelope, utc_now
from glite_english_audit.artifacts.hashing import new_artifact_id
from glite_english_audit.artifacts.io import (
    ensure_private_dir,
    read_jsonl_models,
    read_model,
    write_jsonl_models,
    write_model,
)
from glite_english_audit.artifacts.models import (
    EvidenceSpan,
    NormalizedUtterance,
    PrivateMistake,
    PrivateMistakesManifest,
    ReviewedSubmissionArtifact,
    SafeMistakeRecord,
    SafeRecordCandidate,
)
from glite_english_audit.discovery import registry
from glite_english_audit.discovery.inventory import PrivateInventory, summarize
from glite_english_audit.normalization.filter_corpus import filter_corpus
from glite_english_audit.paths import stage_dir
from glite_english_audit.pipeline import (
    apply_authorship,
    authorship_batches,
    batches,
    build_review,
    collect,
    promote_records,
    start_run,
)
from glite_english_audit.submission.package import materialize_package
from glite_english_audit.verification.deterministic import (
    verify_package_against_review,
    verify_submission_package,
)
from glite_english_audit.verification.verify_corpus import verify_corpus

_FIXTURE_HOME = (
    Path(__file__).resolve().parent.parent / "fixtures" / "claude_code" / "success" / "home"
)


@pytest.fixture
def only_claude_code() -> None:
    """Register a single adapter so the run is independent of the registry."""
    if "claude_code" not in registry.adapter_ids():
        registry.register_adapter("claude_code", claude_code_adapter)


def _stand_in_decision(candidate: dict[str, str]) -> dict[str, object]:
    """A deterministic authorship judgment, standing in for the model.

    The rules are arbitrary but fixed, so the run is reproducible: material
    about the deploy script is treated as pasted, a candidate carrying markup
    keeps only what follows it, and everything else is the learner's own.
    """
    text = candidate["text"]
    if "deploy script" in text:
        return {
            "utterance_id": candidate["utterance_id"],
            "decision": "exclude",
            "retained_spans": [],
            "reason": "AUTHORSHIP_PASTED_MATERIAL",
        }
    tail = text[text.rindex(">") + 1 :].strip() if ">" in text else ""
    if tail:
        return {
            "utterance_id": candidate["utterance_id"],
            "decision": "partial",
            "retained_spans": [tail],
            "reason": "AUTHORSHIP_AGENT_MACHINERY",
        }
    return {
        "utterance_id": candidate["utterance_id"],
        "decision": "retain",
        "retained_spans": [text],
        "reason": None,
    }


def _repo_with_ignored_temp(tmp_path: Path) -> Path:
    """A minimal git repository whose temp tree is ignored, for snapshots."""
    import subprocess

    repo = tmp_path / "checkout"
    repo.mkdir()
    (repo / ".gitignore").write_text("temp/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=T", "commit", "-qm", "init"],
        cwd=repo,
        check=True,
    )
    return repo


def test_waterfall_runs_stage_by_stage(tmp_path: Path, only_claude_code: None) -> None:
    runs_root = tmp_path / "runs"
    repo = _repo_with_ignored_temp(tmp_path)

    # Stage 0: discovery against the fixture home, agent-visible output only.
    from datetime import UTC, datetime

    from glite_english_audit.artifacts.enums import OsEnvironment
    from glite_english_audit.discovery.base import DiscoveryContext

    outcome = claude_code_adapter().discover(
        DiscoveryContext(
            os_environment=OsEnvironment.MACOS,
            home=_FIXTURE_HOME,
            now=datetime(2026, 8, 8, tzinfo=UTC),
            environ={},
        )
    )
    inventory_dir = ensure_private_dir(tmp_path / "inventory")
    write_model(
        inventory_dir / "source-inventory.json",
        PrivateInventory(
            records=outcome.records,
            instance_paths={k: str(v) for k, v in outcome.instance_paths.items()},
        ),
    )
    summaries = [summarize(record) for record in outcome.records]
    assert summaries, "discovery must find the fixture instances"
    for summary in summaries:
        blob = json.dumps(summary.model_dump(mode="json"))
        assert "/" not in summary.opaque_label
        assert "home" not in blob

    # Run creation freezes the selection and the record cutoff.
    manifest = start_run.start_run(
        runtime=__import__(
            "glite_english_audit.artifacts.enums", fromlist=["AgentRuntime"]
        ).AgentRuntime.CLAUDE_CODE,
        os_environment_value="macos",
        preset="everything",
        instance_keys=None,
        processing_profile="recommended",
        runs_root=runs_root,
        inventory_dir=inventory_dir,
    )
    run_id = manifest.run_id
    assert manifest.selection is not None
    assert manifest.selection.selected_instance_keys

    # Stages 1-2: snapshot and extract, then the snapshots are gone.
    collected = collect.collect(run_id, runs_root=runs_root, repo=repo)
    assert collected["candidate_utterances"], "extraction produced no candidates"
    assert collected["excluded_instances"] == []
    snapshots = repo / "temp" / "runtime" / run_id / "snapshots"
    leftover = [p for p in snapshots.rglob("*") if p.is_file()] if snapshots.exists() else []
    assert leftover == [], "snapshots must be removed once extraction is durable"

    # Stage 3, fallback path: the pre-filter alone builds a verifiable corpus.
    corpus_manifest = filter_corpus(run_id, runs_root=runs_root)
    assert corpus_manifest.english_word_count > 0
    assert verify_corpus(run_id, runs_root=runs_root) == []

    # Stage 3, model path: candidate batches, a deterministic stand-in for the
    # authorship judgment, then the verifier that counts what it kept.
    candidate_index = authorship_batches.prepare_authorship_batches(
        run_id, batch_size=5, runs_root=runs_root
    )
    assert candidate_index.candidate_count > 0
    candidate_dir = authorship_batches.batch_dir(run_id, runs_root=runs_root)
    decisions_dir = authorship_batches.decisions_dir(run_id, runs_root=runs_root)
    planned = {"retain": 0, "partial": 0, "exclude": 0}
    for candidate_path in sorted(candidate_dir.glob("batch-*.jsonl")):
        decisions = [
            _stand_in_decision(json.loads(line))
            for line in candidate_path.read_text(encoding="utf-8").splitlines()
        ]
        for decision in decisions:
            planned[str(decision["decision"])] += 1
        name = candidate_path.name.replace("batch-", "decisions-")
        (decisions_dir / name).write_text(
            "\n".join(json.dumps(decision, ensure_ascii=False) for decision in decisions) + "\n",
            encoding="utf-8",
        )
    applied = apply_authorship.apply_authorship(run_id, runs_root=runs_root)
    assert applied.diagnostics == []
    assert applied.quarantined_decisions == 0
    assert applied.missing_decisions == 0
    assert (applied.retained, applied.partial, applied.excluded) == (
        planned["retain"],
        planned["partial"],
        planned["exclude"],
    )
    assert applied.excluded >= 1
    assert applied.partial >= 1
    # The model dropped material the pre-filter had to keep, so the
    # analyzed-word denominator is strictly smaller than what it was offered.
    assert 0 < applied.words_after < applied.words_before
    corpus_manifest = applied.manifest
    assert verify_corpus(run_id, runs_root=runs_root) == []

    # Stage 4 input: deterministic batches.
    batch_result = batches.prepare_batches(run_id, batch_size=5, runs_root=runs_root)
    assert int(str(batch_result["batches"])) >= 1
    batch_dir = Path(str(batch_result["batch_dir"]))
    first = sorted(batch_dir.glob("batch-*.jsonl"))[0]
    rows = [json.loads(line) for line in first.read_text().splitlines()]
    assert {"utterance_id", "text", "modality"} == set(rows[0])

    # Stage 5: a deterministic stand-in for the semantic producer.
    corpus = list(
        read_jsonl_models(
            stage_dir(run_id, StageId.ELIGIBLE_ENGLISH, root=runs_root) / "corpus.jsonl",
            NormalizedUtterance,
        )
    )
    target = next(u for u in corpus if "very like" in u.text)
    phrase = "I very like this plan"
    start = target.text.find(phrase)
    mistakes_dir = ensure_private_dir(stage_dir(run_id, StageId.PRIVATE_MISTAKES, root=runs_root))
    mistake = PrivateMistake(
        mistake_id="m-1",
        occurrence_id="m-1-o1",
        finding_artifact_id=new_artifact_id(),
        utterance_id=target.utterance_id,
        evidence_span=EvidenceSpan(start=start, end=start + len(phrase)),
        original_text=phrase,
        correction="I really like this plan",
        explanation="'Very' cannot directly modify the verb 'like'.",
        modality=Modality.WRITTEN,
        source_adapter="claude_code",
        session_hash=target.session_hash,
    )
    # A second verified occurrence, so every safe-record candidate below has a
    # private mistake behind it and the count arithmetic can balance.
    second = mistake.model_copy(
        update={
            "mistake_id": "m-2",
            "occurrence_id": "m-2-o1",
            "original_text": "depends from",
            "correction": "depends on",
            "explanation": "The verb 'depend' takes 'on', not 'from'.",
        }
    )
    mistakes_path = mistakes_dir / "mistakes.jsonl"
    write_jsonl_models(mistakes_path, [mistake, second])
    write_model(
        mistakes_dir / "private-mistakes-manifest.json",
        PrivateMistakesManifest(
            envelope=ArtifactEnvelope(
                schema_name="private_mistakes",
                schema_version=1,
                artifact_id=new_artifact_id(),
                run_id=run_id,
                stage_id=StageId.PRIVATE_MISTAKES,
                producer_name="test",
                producer_version="1.0.0",
                created_at=utc_now(),
            ),
            mistake_count=2,
            jsonl_relative_path="mistakes.jsonl",
            jsonl_sha256=__import__(
                "glite_english_audit.artifacts.hashing", fromlist=["sha256_hex"]
            ).sha256_hex(mistakes_path.read_bytes()),
        ),
    )

    # Stage 6: one safe candidate and one that must be withheld.
    safe_dir = ensure_private_dir(stage_dir(run_id, StageId.SAFE_RECORDS, root=runs_root))
    good = SafeRecordCandidate(
        mistake_id="m-1",
        record=SafeMistakeRecord(
            mistake="Used 'very' to modify a verb directly.",
            rule="In English, 'very' cannot modify a verb; use 'really' instead.",
            example="I really like this plan.",
            example_type=ExampleType.SYNTHETIC,
            source_type="claude_code",
            modality=Modality.WRITTEN,
        ),
        creator_version="1.0.0",
    )
    leaky = SafeRecordCandidate(
        mistake_id="m-2",
        record=SafeMistakeRecord(
            mistake="Used the wrong preposition.",
            rule="The verb 'depend' takes 'on', not 'from'.",
            example="Mail the result to someone@example.com when it depends from the input.",
            example_type=ExampleType.SYNTHETIC,
            source_type="claude_code",
            modality=Modality.WRITTEN,
        ),
        creator_version="1.0.0",
    )
    write_jsonl_models(safe_dir / "candidates.jsonl", [good, leaky])

    # Stage 7: the deterministic scanner promotes only the safe record.
    promoted = promote_records.promote(run_id, runs_root=runs_root)
    assert promoted["approved"] == 1
    assert promoted["withheld_for_privacy"] == 1

    # Stage 8: counts computed from the run's own artifacts.
    reviewed = build_review.build_review(run_id, runs_root=runs_root)
    counts = reviewed.counts
    assert len(reviewed.records) == 1
    assert all(record.included for record in reviewed.records)
    assert counts.shared_mistakes == 1
    assert counts.withheld_by_user == 0
    withheld_total = (
        counts.withheld_by_user + counts.withheld_for_privacy + sum(counts.other_withheld.values())
    )
    assert counts.shared_mistakes + withheld_total == counts.verified_total_mistakes
    assert counts.analyzed_english_words == corpus_manifest.english_word_count

    # The artifact on disk is what the review server would load.
    stored = read_model(
        stage_dir(run_id, StageId.REVIEWED_SUBMISSION, root=runs_root) / "reviewed-submission.json",
        ReviewedSubmissionArtifact,
    )
    assert stored.counts == counts

    # And it materializes into a package that passes the full gate.
    package = materialize_package(stored)
    assert verify_submission_package(package) == []
    assert verify_package_against_review(package, stored) == []
    assert len(package.records) == 1

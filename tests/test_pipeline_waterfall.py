"""The whole waterfall must run step by step through its own drivers.

This is the test the project lacked: every step executed through the module
an agent would actually invoke, over committed fixtures, with a deterministic
stand-in for the semantic steps. It fails if any step driver stops being
runnable, not merely if a function returns the wrong value.

Three of the nine stages this replaced are no longer steps at all: the source
inventory, the snapshot manifests, and the review. They produce one artifact
for the whole run rather than one file per session, so they are exercised
here through the run-level paths that own them rather than through a step
directory.
"""

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from confidentiality_stub import write_confidentiality_report
from glite_english_audit.adapters.claude_code import create_adapter as claude_code_adapter
from glite_english_audit.artifacts.enums import (
    ExampleType,
    Modality,
    Stability,
    StageStatus,
    StepId,
)
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
    EligibleCorpusManifest,
    EvidenceSpan,
    NormalizedUtterance,
    PrivateMistake,
    PrivateMistakesManifest,
    ReviewedSubmissionArtifact,
    SafeMistakeRecord,
    SafeRecordCandidate,
    SourceInstanceRecord,
)
from glite_english_audit.diagnostics.codes import Diagnostic
from glite_english_audit.discovery import registry
from glite_english_audit.discovery.base import (
    DiscoveryContext,
    DiscoveryOutcome,
    SnapshotCapture,
)
from glite_english_audit.discovery.inventory import PrivateInventory, summarize
from glite_english_audit.normalization.filter_corpus import filter_corpus
from glite_english_audit.paths import inventory_path, snapshot_dir, step_dir, submission_dir
from glite_english_audit.pipeline import (
    apply_authorship,
    authorship_batches,
    batches,
    build_review,
    collect,
    deduplicate,
    promote_records,
    start_run,
)
from glite_english_audit.pipeline.record_stage import advance_to
from glite_english_audit.sessions import read_index, session_files
from glite_english_audit.submission.package import materialize_package
from glite_english_audit.verification.deterministic import (
    verify_package_against_review,
    verify_submission_package,
)
from glite_english_audit.verification.verify_corpus import verify_corpus

_FIXTURE_HOME = (
    Path(__file__).resolve().parent.parent / "fixtures" / "claude_code" / "success" / "home"
)

_NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


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
    (repo / ".gitignore").write_text("runtime/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=T", "commit", "-qm", "init"],
        cwd=repo,
        check=True,
    )
    return repo


def _seeded_run(tmp_path: Path, runs_root: Path) -> str:
    """Discovery plus run creation over the fixture home, without the stages."""
    from glite_english_audit.artifacts.enums import AgentRuntime, OsEnvironment
    from glite_english_audit.discovery.base import DiscoveryContext

    outcome = claude_code_adapter().discover(
        DiscoveryContext(
            os_environment=OsEnvironment.MACOS,
            home=_FIXTURE_HOME,
            now=datetime(2026, 8, 8, tzinfo=UTC),
            environ={},
        )
    )
    inventory_dir = ensure_private_dir(tmp_path / "inventory-2")
    write_model(
        inventory_dir / "source-inventory.json",
        PrivateInventory(
            records=outcome.records,
            instance_paths={k: str(v) for k, v in outcome.instance_paths.items()},
            created_at=_NOW,
        ),
    )
    manifest = start_run.start_run(
        runtime=AgentRuntime.CLAUDE_CODE,
        os_environment_value="macos",
        preset="everything",
        instance_keys=None,
        processing_profile="recommended",
        runs_root=runs_root,
        inventory_dir=inventory_dir,
        local_scan_consent=True,
        provider_transfer_consent=True,
    )
    return manifest.run_id


def test_waterfall_runs_step_by_step(tmp_path: Path, only_claude_code: None) -> None:
    runs_root = tmp_path / "runs"
    repo = _repo_with_ignored_temp(tmp_path)

    # Discovery is not a step: it describes the machine rather than any one
    # session. Its agent-visible output is summaries only.
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
            created_at=_NOW,
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
        local_scan_consent=True,
        provider_transfer_consent=True,
    )
    run_id = manifest.run_id
    assert manifest.selection is not None
    assert manifest.selection.selected_instance_keys
    # The run's own copy of the inventory is a single file at the run root,
    # beside the manifest. It has no per-session file to sit next to, so it is
    # the one artifact of the old stage 0 that survived the five-step layout.
    assert inventory_path(run_id, root=runs_root).is_file()

    # Step a: snapshot and extract. One session is one file from here on, named
    # by an opaque sequence number, with the index the only place a file name
    # is tied to a session hash.
    collected = collect.collect(run_id, runs_root=runs_root, repo=repo)
    assert collected["candidate_utterances"], "extraction produced no candidates"
    assert collected["excluded_instances"] == []
    step_a = step_dir(run_id, StepId.A_COLLECTED, root=runs_root)
    collected_names = [path.name for path in session_files(step_a)]
    assert len(collected_names) == collected["sessions"]
    assert set(read_index(step_a)) == set(collected_names)

    # The snapshots are verbatim copies of the user's application data, so they
    # must be gone the moment extraction is durable. The path this checked was
    # a directory no run has ever written, so it passed whether or not anything
    # had been deleted; it is asked of the real snapshot location now.
    snapshots = snapshot_dir(run_id, repo=repo)
    leftover = [p for p in snapshots.rglob("*") if p.is_file()] if snapshots.exists() else []
    assert leftover == [], "snapshots must be removed once extraction is durable"

    # Step b: deduplication is comparison rather than judgment, so it runs
    # before any model spends tokens on text that is about to be discarded. It
    # hands step c back the same file set it was given — including a session
    # whose every message was a duplicate, which stays as an empty file,
    # because a missing file and an emptied one mean different things.
    deduplicated = deduplicate.deduplicate(run_id, runs_root=runs_root)
    step_b = step_dir(run_id, StepId.B_DEDUPLICATED, root=runs_root)
    assert [path.name for path in session_files(step_b)] == collected_names
    assert read_index(step_b) == read_index(step_a)
    assert deduplicated["messages_in"] == collected["candidate_utterances"]
    # Step b's files are no longer a faithful record of the sessions they name,
    # so what it dropped is written down rather than left to be inferred.
    assert (step_b / "removed.json").is_file()

    # Step c, fallback path: the pre-filter alone builds a verifiable corpus.
    corpus_manifest = filter_corpus(run_id, runs_root=runs_root)
    assert corpus_manifest.english_word_count > 0
    assert verify_corpus(run_id, runs_root=runs_root) == []

    # Step c, model path: candidate batches, a deterministic stand-in for the
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

    # Step d input: deterministic batches.
    batch_result = batches.prepare_batches(run_id, batch_size=5, runs_root=runs_root)
    assert int(str(batch_result["batches"])) >= 1
    batch_dir = Path(str(batch_result["batch_dir"]))
    first = sorted(batch_dir.glob("batch-*.jsonl"))[0]
    rows = [json.loads(line) for line in first.read_text().splitlines()]
    assert {"utterance_id", "text", "modality"} == set(rows[0])

    # Step d is where three of the old stages ended up: the findings a model
    # writes from those batches, the private mistake records made from the
    # findings, and the privacy-safe candidates made from the records. Nothing
    # below reads the findings, so the stand-in produces the two artifacts that
    # are read, and writes them into the one directory that now owns all three.
    mistakes_dir = ensure_private_dir(step_dir(run_id, StepId.D_MISTAKES, root=runs_root))

    # The private mistakes: a deterministic stand-in for the semantic producer.
    corpus = list(
        read_jsonl_models(
            step_dir(run_id, StepId.C_AUTHORED, root=runs_root) / "corpus.jsonl",
            NormalizedUtterance,
        )
    )
    target = next(u for u in corpus if "very like" in u.text)
    phrase = "I very like this plan"
    start = target.text.find(phrase)
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
    # private mistake behind it and the count arithmetic can balance. It cites a
    # different utterance rather than copying this one's span: two records over
    # the same characters is double counting, and the mistake verifier rejects
    # it. The earlier version of this stand-in did exactly that, and also quoted
    # text its own span did not contain.
    other = next(u for u in corpus if u.utterance_id != target.utterance_id and len(u.text) > 12)
    second = mistake.model_copy(
        update={
            "mistake_id": "m-2",
            "occurrence_id": "m-2-o1",
            "utterance_id": other.utterance_id,
            "session_hash": other.session_hash,
            "evidence_span": EvidenceSpan(start=0, end=12),
            "original_text": other.text[0:12],
            "correction": "a natural rewriting",
            "explanation": "A second occurrence, in a different message.",
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
                stage_id=StepId.D_MISTAKES,
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

    # The privacy-safe candidates, in that same step directory: one that is
    # safe to publish as written and one that must be withheld.
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
    write_jsonl_models(mistakes_dir / "candidates.jsonl", [good, leaky])

    # Step e, first half: the independent semantic verifier. It is a model in
    # production and a stand-in here, but its report is not optional — promotion
    # refuses any candidate it does not clear, so skipping it can no longer
    # produce a package that claims two gates passed.
    write_confidentiality_report(run_id, ["m-1", "m-2"], runs_root=runs_root)

    # Step e, second half: the deterministic scanner promotes only the record
    # that also survives its patterns. Promoting is also what records steps d
    # and e as done, which is why no stand-in promotion is needed for the
    # findings the semantic producer did not write here.
    promoted = promote_records.promote(run_id, runs_root=runs_root)
    assert promoted["approved"] == 1
    assert promoted["withheld_for_privacy"] == 1

    # The review is not a step: it produces one artifact for the whole run and
    # then waits on a person. Its counts are computed from the run's own
    # artifacts, and it refuses to run at all unless every step is promoted.
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

    # The artifact on disk is what the review server would load. It lives in
    # the run's submission directory rather than in a step directory.
    stored = read_model(
        submission_dir(run_id, root=runs_root) / "reviewed-submission.json",
        ReviewedSubmissionArtifact,
    )
    assert stored.counts == counts

    # And it materializes into a package that passes the full gate.
    package = materialize_package(stored)
    assert verify_submission_package(package) == []
    assert verify_package_against_review(package, stored) == []
    assert len(package.records) == 1


def test_an_adapter_that_fails_its_own_checks_loses_only_its_own_source(
    tmp_path: Path, only_claude_code: None
) -> None:
    """Every adapter's verify() was dead code in production.

    Several hundred lines across nine adapters — the duplicate-ID checks, the
    belongs-to-this-adapter checks, and the opened-path audits that emit
    SOURCE_SNAPSHOT_UNSAFE_PATH — were called only by tests, so no structural
    defect an adapter could detect was able to fail a run.
    """
    runs_root = tmp_path / "runs"
    repo = _repo_with_ignored_temp(tmp_path)
    run_id = _seeded_run(tmp_path, runs_root)

    real = claude_code_adapter()

    class _FailsItsOwnCheck:
        """Extracts normally, then reports an error about what it extracted."""

        @property
        def adapter_id(self) -> str:
            return real.adapter_id

        @property
        def adapter_version(self) -> str:
            return real.adapter_version

        @property
        def stability(self) -> Stability:
            return real.stability

        def discover(self, context: DiscoveryContext) -> DiscoveryOutcome:
            return real.discover(context)

        def snapshot(
            self, instance: SourceInstanceRecord, source_path: Path, target_dir: Path
        ) -> SnapshotCapture:
            return real.snapshot(instance, source_path, target_dir)

        def extract(
            self, instance: SourceInstanceRecord, snapshot_dir: Path
        ) -> Iterator[NormalizedUtterance]:
            return real.extract(instance, snapshot_dir)

        def verify(
            self, instance: SourceInstanceRecord, utterances: list[NormalizedUtterance]
        ) -> list[Diagnostic]:
            return [
                Diagnostic.from_code(
                    "SOURCE_SNAPSHOT_UNSAFE_PATH",
                    "the adapter opened a file outside its allowlist",
                    item_ref="synthetic",
                )
            ]

    # The registry refuses to re-register, so swap the factory directly and
    # restore it: this test is about the pipeline calling verify(), not about
    # the registry's own rules.
    registry._FACTORIES["claude_code"] = lambda: _FailsItsOwnCheck()
    try:
        collected = collect.collect(run_id, runs_root=runs_root, repo=repo)
    finally:
        registry._FACTORIES["claude_code"] = claude_code_adapter

    assert collected["candidate_utterances"] == 0
    entries = cast(list[dict[str, str]], collected["excluded_instances"])
    reasons = [entry["reason"] for entry in entries]
    assert reasons and all("SOURCE_SNAPSHOT_UNSAFE_PATH" in reason for reason in reasons)


def test_the_review_reports_the_utterances_it_could_not_judge(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], only_claude_code: None
) -> None:
    """Coverage loss must not hide inside a denominator.

    Utterances step c quarantines never become eligible, so they are absent
    from every count the review shows. A run that failed to read a third of the
    input would report a rate over the surviving two thirds and say nothing
    about the rest.
    """
    runs_root = tmp_path / "runs"
    repo = _repo_with_ignored_temp(tmp_path)
    run_id = _seeded_run(tmp_path, runs_root)
    collect.collect(run_id, runs_root=runs_root, repo=repo)
    deduplicate.deduplicate(run_id, runs_root=runs_root)
    filter_corpus(run_id, runs_root=runs_root)

    corpus_dir = step_dir(run_id, StepId.C_AUTHORED, root=runs_root)
    manifest = read_model(corpus_dir / "eligible-corpus-manifest.json", EligibleCorpusManifest)
    write_model(
        corpus_dir / "eligible-corpus-manifest.json",
        manifest.model_copy(update={"quarantined_utterance_count": 7}),
    )

    # Both files belong to step d now: the mistakes the review counts and the
    # safe-record candidates promotion reads. Empty, because this test is about
    # what the review says when there is nothing to report.
    mistakes_dir = ensure_private_dir(step_dir(run_id, StepId.D_MISTAKES, root=runs_root))
    write_jsonl_models(mistakes_dir / "mistakes.jsonl", [])
    write_jsonl_models(mistakes_dir / "candidates.jsonl", [])
    write_confidentiality_report(run_id, [], runs_root=runs_root)
    # Steps a and b promote themselves and promote_records promotes d and e, so
    # step c is the only one left: filter_corpus is the fallback path and
    # records nothing, so its promotion is a stand-in written here.
    advance_to(run_id, StepId.C_AUTHORED, StageStatus.PROMOTED, runs_root=runs_root)
    promote_records.promote(run_id, runs_root=runs_root)

    build_review.main(["--run-id", run_id, "--runs-root", str(runs_root)])
    reported = json.loads(capsys.readouterr().out)
    assert reported["unjudged_utterances"] == 7


def test_a_source_that_fails_verification_is_reported_once(
    tmp_path: Path, only_claude_code: None
) -> None:
    """One instance, one exclusion entry.

    The failure path deletes the snapshot and then records the exclusion. An
    earlier ordering recorded it first, so a cleanup that raised produced two
    entries for the same instance and a count that overstated how many sources
    were lost.
    """
    from glite_english_audit.diagnostics.codes import Diagnostic as _Diagnostic

    runs_root = tmp_path / "runs"
    repo = _repo_with_ignored_temp(tmp_path)
    run_id = _seeded_run(tmp_path, runs_root)
    real = claude_code_adapter()

    class _AlwaysFails:
        @property
        def adapter_id(self) -> str:
            return real.adapter_id

        @property
        def adapter_version(self) -> str:
            return real.adapter_version

        @property
        def stability(self) -> Stability:
            return real.stability

        def discover(self, context: DiscoveryContext) -> DiscoveryOutcome:
            return real.discover(context)

        def snapshot(
            self, instance: SourceInstanceRecord, source_path: Path, target_dir: Path
        ) -> SnapshotCapture:
            return real.snapshot(instance, source_path, target_dir)

        def extract(
            self, instance: SourceInstanceRecord, snapshot_dir: Path
        ) -> Iterator[NormalizedUtterance]:
            return real.extract(instance, snapshot_dir)

        def verify(
            self, instance: SourceInstanceRecord, utterances: list[NormalizedUtterance]
        ) -> list[_Diagnostic]:
            return [_Diagnostic.from_code("SOURCE_SNAPSHOT_UNSAFE_PATH", "denylisted path")]

    registry._FACTORIES["claude_code"] = lambda: _AlwaysFails()
    try:
        collected = collect.collect(run_id, runs_root=runs_root, repo=repo)
    finally:
        registry._FACTORIES["claude_code"] = claude_code_adapter

    entries = cast(list[dict[str, str]], collected["excluded_instances"])
    instances = [entry["instance"] for entry in entries]
    assert len(instances) == len(set(instances)), f"an instance was excluded twice: {instances}"


def test_the_review_refuses_a_run_whose_mistakes_double_count(
    tmp_path: Path, only_claude_code: None
) -> None:
    """verified_total_mistakes is len(mistakes).

    A step-d record covering text another record already covers inflates the
    learner's error rate, and nothing further down can tell. The orchestration
    is told to run the verifier; running it inside build_review is what makes
    the count true rather than merely checked by someone who might have skipped
    a step.
    """
    runs_root = tmp_path / "runs"
    repo = _repo_with_ignored_temp(tmp_path)
    run_id = _seeded_run(tmp_path, runs_root)
    collect.collect(run_id, runs_root=runs_root, repo=repo)
    deduplicate.deduplicate(run_id, runs_root=runs_root)
    filter_corpus(run_id, runs_root=runs_root)

    corpus = list(
        read_jsonl_models(
            step_dir(run_id, StepId.C_AUTHORED, root=runs_root) / "corpus.jsonl",
            NormalizedUtterance,
        )
    )
    target = next(u for u in corpus if len(u.text) > 20)
    wide = PrivateMistake(
        mistake_id="m-1",
        occurrence_id="m-1-o1",
        finding_artifact_id=new_artifact_id(),
        utterance_id=target.utterance_id,
        evidence_span=EvidenceSpan(start=0, end=20),
        original_text=target.text[0:20],
        correction="a natural rewriting",
        explanation="A construction that needs rewriting.",
        modality=Modality.WRITTEN,
        source_adapter="claude_code",
        session_hash=target.session_hash,
    )
    nested = wide.model_copy(
        update={
            "mistake_id": "m-2",
            "occurrence_id": "m-2-o1",
            "evidence_span": EvidenceSpan(start=5, end=12),
            "original_text": target.text[5:12],
        }
    )
    mistakes_dir = ensure_private_dir(step_dir(run_id, StepId.D_MISTAKES, root=runs_root))
    write_jsonl_models(mistakes_dir / "mistakes.jsonl", [wide, nested])
    write_jsonl_models(mistakes_dir / "candidates.jsonl", [])
    write_confidentiality_report(run_id, [], runs_root=runs_root)
    advance_to(run_id, StepId.C_AUTHORED, StageStatus.PROMOTED, runs_root=runs_root)
    promote_records.promote(run_id, runs_root=runs_root)

    with pytest.raises(ValueError, match="fail their own verifier"):
        build_review.build_review(run_id, runs_root=runs_root)

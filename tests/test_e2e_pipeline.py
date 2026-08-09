"""End-to-end deterministic pipeline test with a fake semantic producer.

Runs the real stage chain over the committed synthetic fixtures: discovery,
snapshot, extraction, normalization, findings production (faked
deterministically in place of the model), private mistake structuring, safe
record creation, privacy scanning, review, and package materialization. No
model and no network are involved anywhere.
"""

from datetime import UTC, datetime
from pathlib import Path

from glite_english_audit.adapters.claude_code import create_adapter
from glite_english_audit.artifacts.enums import (
    ExampleType,
    Modality,
    OsEnvironment,
    StepId,
)
from glite_english_audit.artifacts.envelope import ArtifactEnvelope
from glite_english_audit.artifacts.hashing import new_artifact_id, new_run_id, sha256_hex
from glite_english_audit.artifacts.models import (
    AuditCounts,
    EvidenceSpan,
    FindingsArtifactMeta,
    ModalityCounts,
    NormalizedUtterance,
    PrivateMistake,
    ReviewedRecord,
    ReviewedSubmissionArtifact,
    SafeMistakeRecord,
)
from glite_english_audit.discovery.base import DiscoveryContext
from glite_english_audit.normalization.authorship import strip_non_authored
from glite_english_audit.normalization.dedup import dedupe
from glite_english_audit.normalization.language import classify_english
from glite_english_audit.normalization.tokenizer import count_words
from glite_english_audit.submission.capability import detect_capability
from glite_english_audit.submission.package import materialize_package
from glite_english_audit.verification.deterministic import (
    verify_package_against_review,
    verify_submission_package,
)
from glite_english_audit.verification.findings_format import (
    EMPTY_RESULT_LINE,
    THRESHOLD_LINE,
    TITLE_LINE,
    verify_findings_artifact,
)
from glite_english_audit.verification.privacy_scanner import scan_safe_record

_FIXTURE_HOME = (
    Path(__file__).resolve().parent.parent / "fixtures" / "claude_code" / "success" / "home"
)


def _envelope(run_id: str, stage: StepId, name: str) -> ArtifactEnvelope:
    return ArtifactEnvelope(
        schema_name=name,
        schema_version=1,
        artifact_id=new_artifact_id(),
        run_id=run_id,
        stage_id=stage,
        producer_name="e2e-test",
        producer_version="1.0.0",
        created_at=datetime(2026, 8, 8, tzinfo=UTC),
    )


def test_full_deterministic_pipeline(tmp_path: Path) -> None:
    run_id = new_run_id()

    # Stages 0-2: discovery, snapshot, extraction over the committed fixture.
    adapter = create_adapter()
    context = DiscoveryContext(
        os_environment=OsEnvironment.MACOS,
        home=_FIXTURE_HOME,
        now=datetime(2026, 8, 8, tzinfo=UTC),
        environ={},
    )
    outcome = adapter.discover(context)
    found = [r for r in outcome.records if r.candidate_messages > 0]
    assert found, "fixture must yield at least one instance with candidates"
    utterances: list[NormalizedUtterance] = []
    for instance in found:
        snapshot_dir = tmp_path / "snapshots" / instance.instance_key[:12]
        snapshot_dir.mkdir(parents=True)
        adapter.snapshot(instance, outcome.instance_paths[instance.instance_key], snapshot_dir)
        utterances.extend(adapter.extract(instance, snapshot_dir))
    assert utterances

    # Stage 3: authorship, language, dedup, counting.
    eligible = []
    for utterance in utterances:
        cleaned = strip_non_authored(utterance.text).cleaned_text.strip()
        if not cleaned:
            continue
        decision = classify_english(cleaned)
        if decision.quarantined or decision.english_text is None:
            continue
        eligible.append(utterance.model_copy(update={"text": decision.english_text}))
    canonical = dedupe(eligible).canonical
    assert canonical
    analyzed_words = sum(count_words(u.text) for u in canonical)
    assert analyzed_words > 0

    # Stage 4: fake deterministic producer flags the planted construction.
    target = next(u for u in canonical if "very like" in u.text)
    original = "I very like this plan"
    start = target.text.find(original)
    assert start >= 0
    body = (
        f"{TITLE_LINE}\n\n{THRESHOLD_LINE}\n\n"
        "## Finding 1\n\n"
        f"Original: {original}\n"
        "Correction: I really like this plan\n"
        "Why: 'Very' cannot directly modify the verb 'like'.\n"
    )
    body_path = tmp_path / "findings" / f"{target.utterance_id}.md"
    body_path.parent.mkdir(parents=True)
    body_path.write_text(body, encoding="utf-8")
    meta = FindingsArtifactMeta(
        envelope=_envelope(run_id, StepId.D_MISTAKES, "plain_findings"),
        unit_id=target.utterance_id,
        utterance_ids=[target.utterance_id],
        finding_count=1,
        no_mistakes_found=False,
        body_relative_path=body_path.name,
        body_sha256=sha256_hex(body.encode("utf-8")),
    )
    assert verify_findings_artifact(body_path, meta, item_ref=target.utterance_id) == []

    # Empty-result form also validates.
    empty_body = f"{TITLE_LINE}\n\n{THRESHOLD_LINE}\n\n{EMPTY_RESULT_LINE}\n"
    empty_path = tmp_path / "findings" / "empty.md"
    empty_path.write_text(empty_body, encoding="utf-8")
    empty_meta = FindingsArtifactMeta(
        envelope=_envelope(run_id, StepId.D_MISTAKES, "plain_findings"),
        unit_id="unit-empty",
        utterance_ids=["unit-empty"],
        finding_count=0,
        no_mistakes_found=True,
        body_relative_path=empty_path.name,
        body_sha256=sha256_hex(empty_body.encode("utf-8")),
    )
    assert verify_findings_artifact(empty_path, empty_meta, item_ref="unit-empty") == []

    # Stage 5: one private structured mistake with an exact evidence span.
    mistake = PrivateMistake(
        mistake_id="m-1",
        occurrence_id="m-1-o1",
        finding_artifact_id=meta.envelope.artifact_id,
        utterance_id=target.utterance_id,
        evidence_span=EvidenceSpan(start=start, end=start + len(original)),
        original_text=original,
        correction="I really like this plan",
        explanation="'Very' cannot directly modify the verb 'like'.",
        modality=Modality.WRITTEN,
        source_adapter="claude_code",
        session_hash=target.session_hash,
    )
    assert target.text[mistake.evidence_span.start : mistake.evidence_span.end] == original

    # Stage 6-7: safe record passes the deterministic privacy scanner.
    safe_record = SafeMistakeRecord(
        mistake="Used 'very' to modify the verb 'like' directly.",
        rule="In English, 'very' cannot modify a verb; use 'really' or 'very much'.",
        example="I very like this plan.",
        example_type=ExampleType.SYNTHETIC,
        source_type="claude_code",
        modality=Modality.WRITTEN,
    )
    assert scan_safe_record(safe_record, item_ref=mistake.mistake_id) == []

    # Stage 8: review, materialization, and the full deterministic gate.
    counts = AuditCounts(
        eligible_english_words=analyzed_words,
        analyzed_english_words=analyzed_words,
        eligible_utterances=len(canonical),
        analyzed_utterances=len(canonical),
        written=ModalityCounts(
            eligible_words=analyzed_words,
            analyzed_words=analyzed_words,
            eligible_utterances=len(canonical),
            analyzed_utterances=len(canonical),
        ),
        spoken_asr=ModalityCounts(
            eligible_words=0, analyzed_words=0, eligible_utterances=0, analyzed_utterances=0
        ),
        verified_total_mistakes=1,
        shared_mistakes=1,
        withheld_by_user=0,
        withheld_for_privacy=0,
    )
    reviewed = ReviewedSubmissionArtifact(
        envelope=_envelope(run_id, StepId.E_VERIFIED, "reviewed_submission"),
        records=[
            ReviewedRecord(
                mistake_id=mistake.mistake_id,
                record=safe_record,
                included=True,
                privacy_creator_version="1.0.0",
                privacy_verifier_version="1.0.0",
            )
        ],
        counts=counts,
    )
    package = materialize_package(reviewed)
    assert verify_submission_package(package) == []
    assert verify_package_against_review(package, reviewed) == []

    # Download-only capability with no endpoint configured.
    capability = detect_capability(tmp_path)
    assert capability.direct_submission_available is False

    # The exported package survives a strict JSON round trip.
    round_tripped = package.model_validate_json(package.model_dump_json())
    assert round_tripped == package

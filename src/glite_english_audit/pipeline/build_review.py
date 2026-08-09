"""CLI: stage 8 — assemble the reviewed submission artifact.

Run: ``uv run python -m glite_english_audit.pipeline.build_review --run-id <id>``

Computes the count set from the run's own artifacts rather than trusting any
stage to report its own totals, then writes the private
:class:`ReviewedSubmissionArtifact` the review server reads. Every record
starts included, which is what the review page shows by default; the user's
exclusions are recorded by the server afterwards.

The counts are the honesty guarantee of the whole audit (specification, 5.6):
the analyzed-word denominator comes from the eligible corpus, the verified
total comes from the private mistakes, and shared plus every withheld class
must add up to that total.
"""

import argparse
import json
import sys
from pathlib import Path

from glite_english_audit import CLIENT_VERSION
from glite_english_audit.artifacts.enums import Modality, StepId
from glite_english_audit.artifacts.envelope import ArtifactEnvelope, utc_now
from glite_english_audit.artifacts.hashing import new_artifact_id
from glite_english_audit.artifacts.io import (
    ensure_private_dir,
    read_jsonl_models,
    read_model,
    write_model,
)
from glite_english_audit.artifacts.models import (
    AuditCounts,
    EligibleCorpusManifest,
    ModalityCounts,
    NormalizedUtterance,
    PrivateMistake,
    ReviewedRecord,
    ReviewedSubmissionArtifact,
    SafeRecordCandidate,
)
from glite_english_audit.english import plural
from glite_english_audit.normalization.tokenizer import count_words
from glite_english_audit.paths import step_dir, submission_dir
from glite_english_audit.pipeline.record_stage import (
    enter_review,
    require_promoted_through,
)
from glite_english_audit.verification.confidentiality_report import load_report
from glite_english_audit.verification.verify_mistakes import verify_mistakes

CORPUS_NAME = "corpus.jsonl"
CORPUS_MANIFEST_NAME = "eligible-corpus-manifest.json"
MISTAKES_NAME = "mistakes.jsonl"
APPROVED_NAME = "approved.jsonl"
WITHHELD_NAME = "withheld.json"
REVIEWED_NAME = "reviewed-submission.json"
PRODUCER_NAME = "pipeline.build_review"


def _modality_counts(
    corpus: list[NormalizedUtterance], modality: Modality, *, analyzed_ids: set[str]
) -> ModalityCounts:
    subset = [u for u in corpus if u.modality is modality]
    analyzed = [u for u in subset if u.utterance_id in analyzed_ids]
    return ModalityCounts(
        eligible_words=sum(count_words(u.text) for u in subset),
        analyzed_words=sum(count_words(u.text) for u in analyzed),
        eligible_utterances=len(subset),
        analyzed_utterances=len(analyzed),
    )


def build_review(
    run_id: str, *, analyzed_ids: set[str] | None = None, runs_root: Path | None = None
) -> ReviewedSubmissionArtifact:
    """Compute counts from the run's artifacts and write the stage-8 artifact.

    ``analyzed_ids`` names the utterances the semantic stages actually
    processed. Anything eligible but unprocessed is reported as reduced
    coverage rather than silently treated as error-free text.
    """
    require_promoted_through(run_id, StepId.E_VERIFIED, runs_root=runs_root)
    corpus_dir = step_dir(run_id, StepId.C_AUTHORED, root=runs_root)
    corpus = list(read_jsonl_models(corpus_dir / CORPUS_NAME, NormalizedUtterance))
    corpus_manifest = read_model(corpus_dir / CORPUS_MANIFEST_NAME, EligibleCorpusManifest)
    processed = analyzed_ids if analyzed_ids is not None else {u.utterance_id for u in corpus}

    mistakes_path = step_dir(run_id, StepId.D_MISTAKES, root=runs_root) / MISTAKES_NAME
    mistakes = list(read_jsonl_models(mistakes_path, PrivateMistake))

    # verified_total_mistakes is len(mistakes), so a stage-5 record that counts
    # one mistake twice inflates the learner's error rate and nothing further
    # down can tell. The orchestration is told to run this verifier; running it
    # here as well is what makes the count true rather than merely checked by
    # someone who might have skipped a step.
    failures = verify_mistakes(mistakes, {u.utterance_id: u.text for u in corpus})
    if failures:
        codes = ", ".join(sorted({diagnostic.code for diagnostic in failures}))
        msg = (
            f"this run's mistake records fail their own verifier "
            f"({len(failures)} {plural(len(failures), 'problem')}: {codes}), so its "
            "counts would be wrong. Repair stage 5 and run "
            "verification.verify_mistakes until it exits zero."
        )
        raise ValueError(msg)

    # The version stamped on every shared record must name the verifier that
    # actually cleared it. Taking it from the client meant the attestation was
    # true of whatever built the package rather than of anything that read the
    # records, so a run that skipped the semantic verifier produced the same
    # claim as one that passed it.
    confidentiality = load_report(run_id, runs_root=runs_root)
    verifier_version = confidentiality.verifier_version or CLIENT_VERSION

    approved_dir = step_dir(run_id, StepId.E_VERIFIED, root=runs_root)
    approved = list(read_jsonl_models(approved_dir / APPROVED_NAME, SafeRecordCandidate))
    withheld_path = approved_dir / WITHHELD_NAME
    withheld: dict[str, list[str]] = (
        json.loads(withheld_path.read_text(encoding="utf-8")) if withheld_path.is_file() else {}
    )

    analyzed = [u for u in corpus if u.utterance_id in processed]
    analyzed_words = sum(count_words(u.text) for u in analyzed)
    written = _modality_counts(corpus, Modality.WRITTEN, analyzed_ids=processed)
    spoken = _modality_counts(corpus, Modality.SPOKEN_ASR, analyzed_ids=processed)

    verified_total = len(mistakes)
    withheld_for_privacy = len(withheld)
    other_withheld: dict[str, int] = {}
    unaccounted = verified_total - len(approved) - withheld_for_privacy
    if unaccounted > 0:
        # A verified mistake with neither an approved record nor a privacy
        # rejection never reached stage 6; it is reported as a processing
        # failure rather than quietly dropped from the total.
        other_withheld["WITHHELD_PROCESSING_FAILED"] = unaccounted

    counts = AuditCounts(
        eligible_english_words=corpus_manifest.english_word_count,
        analyzed_english_words=analyzed_words,
        eligible_utterances=corpus_manifest.utterance_count,
        analyzed_utterances=len(analyzed),
        written=written,
        spoken_asr=spoken,
        verified_total_mistakes=verified_total,
        shared_mistakes=len(approved),
        withheld_by_user=0,
        withheld_for_privacy=withheld_for_privacy,
        other_withheld=other_withheld,
    )
    artifact = ReviewedSubmissionArtifact(
        envelope=ArtifactEnvelope(
            schema_name="reviewed_submission",
            schema_version=1,
            artifact_id=new_artifact_id(),
            run_id=run_id,
            stage_id=StepId.E_VERIFIED,
            producer_name=PRODUCER_NAME,
            producer_version=CLIENT_VERSION,
            created_at=utc_now(),
        ),
        records=[
            ReviewedRecord(
                mistake_id=candidate.mistake_id,
                record=candidate.record,
                included=True,
                privacy_creator_version=candidate.creator_version,
                privacy_verifier_version=verifier_version,
            )
            for candidate in approved
        ],
        counts=counts,
    )
    target = ensure_private_dir(submission_dir(run_id, root=runs_root))
    write_model(target / REVIEWED_NAME, artifact)
    # The run is now waiting on a person rather than on a stage. Recording that
    # is what lets a resumed run reopen the review instead of rebuilding it.
    enter_review(run_id, runs_root=runs_root)
    return artifact


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Stage 8: build the reviewed submission artifact")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--runs-root", type=Path, default=None, help="test override")
    arguments = parser.parse_args(argv)
    artifact = build_review(arguments.run_id, runs_root=arguments.runs_root)
    counts = artifact.counts
    corpus_manifest = read_model(
        step_dir(arguments.run_id, StepId.C_AUTHORED, root=arguments.runs_root)
        / CORPUS_MANIFEST_NAME,
        EligibleCorpusManifest,
    )
    sys.stdout.write(
        json.dumps(
            {
                "records": len(artifact.records),
                "eligible_english_words": counts.eligible_english_words,
                "analyzed_english_words": counts.analyzed_english_words,
                "eligible_utterances": counts.eligible_utterances,
                "analyzed_utterances": counts.analyzed_utterances,
                "verified_total_mistakes": counts.verified_total_mistakes,
                "shared_mistakes": counts.shared_mistakes,
                "withheld_for_privacy": counts.withheld_for_privacy,
                "other_withheld": counts.other_withheld,
                # Utterances stage 3 could not judge at all. They are absent
                # from every count above, so a rate computed from those counts
                # silently describes a smaller corpus than the user gave. The
                # number stays out of the submission package — it is a fact
                # about this run's processing, not about the learner — and is
                # reported here so the agent can say it out loud.
                "unjudged_utterances": corpus_manifest.quarantined_utterance_count,
                "deduplicated_utterances": corpus_manifest.deduplicated_utterance_count,
            },
            indent=2,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

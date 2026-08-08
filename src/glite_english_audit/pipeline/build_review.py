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
from glite_english_audit.artifacts.enums import Modality, StageId
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
from glite_english_audit.normalization.tokenizer import count_words
from glite_english_audit.paths import stage_dir

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
    corpus_dir = stage_dir(run_id, StageId.ELIGIBLE_ENGLISH, root=runs_root)
    corpus = list(read_jsonl_models(corpus_dir / CORPUS_NAME, NormalizedUtterance))
    corpus_manifest = read_model(corpus_dir / CORPUS_MANIFEST_NAME, EligibleCorpusManifest)
    processed = analyzed_ids if analyzed_ids is not None else {u.utterance_id for u in corpus}

    mistakes_path = stage_dir(run_id, StageId.PRIVATE_MISTAKES, root=runs_root) / MISTAKES_NAME
    mistakes = list(read_jsonl_models(mistakes_path, PrivateMistake))

    approved_dir = stage_dir(run_id, StageId.PRIVACY_APPROVED, root=runs_root)
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
            stage_id=StageId.REVIEWED_SUBMISSION,
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
                privacy_verifier_version=CLIENT_VERSION,
            )
            for candidate in approved
        ],
        counts=counts,
    )
    target = ensure_private_dir(stage_dir(run_id, StageId.REVIEWED_SUBMISSION, root=runs_root))
    write_model(target / REVIEWED_NAME, artifact)
    return artifact


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Stage 8: build the reviewed submission artifact")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--runs-root", type=Path, default=None, help="test override")
    arguments = parser.parse_args(argv)
    artifact = build_review(arguments.run_id, runs_root=arguments.runs_root)
    counts = artifact.counts
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
            },
            indent=2,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

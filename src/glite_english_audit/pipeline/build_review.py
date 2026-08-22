"""CLI: assemble the reviewed submission artifact from step e.

Run: ``uv run python -m glite_english_audit.pipeline.build_review --run-id <id>``

Computes the count set from the run's own session files rather than trusting any
step to report its own totals, then writes the private
:class:`ReviewedSubmissionArtifact` the review server reads. Every record starts
included, which is what the review page shows by default; the user's exclusions
are recorded by the server afterwards.

The counts are the honesty guarantee of the whole audit (specification, 5.6):
the word denominator comes from the step-c files, the verified total is every
step-d record, and shared plus every withheld class must add up to that total.
Step e may only drop records, so what it dropped is what was withheld.

The review is not a step. It produces no per-session file, it waits on a person,
and it lives in the run's ``submission/`` directory.
"""

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path

from glite_english_audit import CLIENT_VERSION
from glite_english_audit.artifacts.enums import Modality, StepId
from glite_english_audit.artifacts.envelope import ArtifactEnvelope, utc_now
from glite_english_audit.artifacts.hashing import new_artifact_id
from glite_english_audit.artifacts.io import (
    ensure_private_dir,
    read_model,
    write_model,
)
from glite_english_audit.artifacts.models import (
    AuditCounts,
    MistakeRecord,
    ModalityCounts,
    NormalizedUtterance,
    ReviewedRecord,
    ReviewedSubmissionArtifact,
)
from glite_english_audit.diagnostics.codes import Diagnostic
from glite_english_audit.english import plural
from glite_english_audit.paths import step_dir, submission_dir
from glite_english_audit.pipeline.authorship import INDEX_NAME as CORPUS_INDEX_NAME
from glite_english_audit.pipeline.authorship import AuthoredCorpusIndex, english_words
from glite_english_audit.pipeline.deduplicate import REMOVED_NAME
from glite_english_audit.pipeline.mistakes import REPORT_NAME, read_records, verify_records
from glite_english_audit.pipeline.record_step import (
    enter_review,
    require_promoted_through,
)
from glite_english_audit.sessions import read_all, session_files
from glite_english_audit.verification.reports import VerificationReport

REVIEWED_NAME = "reviewed-submission.json"
PRODUCER_NAME = "pipeline.build_review"


def _modality_counts(
    corpus: list[NormalizedUtterance], modality: Modality, *, analyzed_ids: set[str]
) -> ModalityCounts:
    subset = [u for u in corpus if u.modality is modality]
    analyzed = [u for u in subset if u.utterance_id in analyzed_ids]
    return ModalityCounts(
        eligible_words=sum(english_words(u.text) for u in subset),
        analyzed_words=sum(english_words(u.text) for u in analyzed),
        eligible_utterances=len(subset),
        analyzed_utterances=len(analyzed),
    )


def _records_for(
    run_id: str, step: StepId, name: str, *, runs_root: Path | None = None
) -> tuple[list[MistakeRecord], list[Diagnostic]]:
    """One session's records from one step, or the reason there are none."""
    path = step_dir(run_id, step, root=runs_root) / name
    if not path.is_file():
        return [], [
            Diagnostic.from_code(
                "LINEAGE_MISSING_INPUT",
                f"step {step.name[0].lower()} has no file for a session step c produced",
                item_ref=name,
            )
        ]
    return read_records(path)


def _cleared_by(run_id: str, step: StepId, *, runs_root: Path | None = None) -> str:
    """The version of the check that passed ``step``, from its own report.

    Taking it from the client instead would make the attestation true of
    whatever built the package rather than of anything that read the records: a
    run that skipped a check produced the same claim as one that passed it.
    """
    path = step_dir(run_id, step, root=runs_root) / REPORT_NAME
    letter = step.name[0].lower()
    if not path.is_file():
        msg = (
            f"step {letter} has no verification report, so nothing has checked its files; "
            f"run the step {letter} driver with --apply until it exits zero"
        )
        raise ValueError(msg)
    report = read_model(path, VerificationReport)
    if not report.passed:
        msg = (
            f"step {letter}'s own verification report says it failed, so its records must "
            "not be shared; repair the step and run its driver again"
        )
        raise ValueError(msg)
    return report.verifier_version


def _unjudged_utterances(run_id: str, *, runs_root: Path | None = None) -> int:
    """Utterances in the step-c sessions that quarantined instead of passing.

    They are absent from every count this module computes, so a rate built from
    those counts describes a smaller corpus than the user gave. The number stays
    out of the submission package — it is a fact about this run's processing,
    not about the learner — and is printed so the agent can say it out loud.
    """
    path = step_dir(run_id, StepId.C_AUTHORED, root=runs_root) / CORPUS_INDEX_NAME
    if not path.is_file():
        return 0
    return read_model(path, AuthoredCorpusIndex).quarantined_utterance_count


def _duplicates_removed(run_id: str, *, runs_root: Path | None = None) -> int:
    """How many messages step b dropped as duplicates, from its own sidecar."""
    path = step_dir(run_id, StepId.B_DEDUPLICATED, root=runs_root) / REMOVED_NAME
    if not path.is_file():
        return 0
    payload = json.loads(path.read_text(encoding="utf-8"))
    removed = payload.get("removed", {}) if isinstance(payload, dict) else {}
    return sum(len(ids) for ids in removed.values()) if isinstance(removed, dict) else 0


def _refuse(failures: list[Diagnostic]) -> None:
    codes = ", ".join(sorted({diagnostic.code for diagnostic in failures}))
    msg = (
        f"this run's mistake records fail their own verifier "
        f"({len(failures)} {plural(len(failures), 'problem')}: {codes}), so its counts "
        "would be wrong. Repair step d and run pipeline.mistakes --apply until it exits zero."
    )
    raise ValueError(msg)


def analyzed_ids_from_steps(run_id: str, *, runs_root: Path | None = None) -> set[str]:
    """The utterances whose sessions step d actually read, by file name.

    The audit stops on evidence rather than a calendar: step d works newest
    first and stops once it has found enough, so the oldest sessions may have a
    step-c file and no step-d file. Those utterances were never analyzed, and
    the counts must say so -- this derives the analyzed set from what step d
    demonstrably produced, which no agent can misreport.
    """
    d_names = {
        path.name for path in session_files(step_dir(run_id, StepId.D_MISTAKES, root=runs_root))
    }
    analyzed: set[str] = set()
    for path, members in read_all(step_dir(run_id, StepId.C_AUTHORED, root=runs_root)):
        if path.name in d_names:
            analyzed.update(u.utterance_id for u in members if u.text.strip())
    return analyzed


def build_review(
    run_id: str, *, analyzed_ids: set[str] | None = None, runs_root: Path | None = None
) -> ReviewedSubmissionArtifact:
    """Compute counts from the run's session files and write the review artifact.

    ``analyzed_ids`` names the utterances the model steps actually processed.
    Anything eligible but unprocessed is reported as reduced coverage rather
    than silently treated as error-free text.
    """
    require_promoted_through(run_id, StepId.E_VERIFIED, runs_root=runs_root)
    sessions = list(read_all(step_dir(run_id, StepId.C_AUTHORED, root=runs_root)))
    if not sessions:
        msg = "step c has no session files, so this run has nothing to review"
        raise ValueError(msg)

    # Step c keeps every item its input had and empties the text of anything the
    # learner did not write. An emptied item carries no English and no author,
    # so it is not eligible; it stays in the file because a vanished item and an
    # emptied one mean different things.
    corpus = [u for _, members in sessions for u in members if u.text.strip()]
    processed = analyzed_ids if analyzed_ids is not None else {u.utterance_id for u in corpus}

    failures: list[Diagnostic] = []
    verified: dict[str, MistakeRecord] = {}
    verified_total = 0
    for path, members in sessions:
        records, diagnostics = _records_for(
            run_id, StepId.D_MISTAKES, path.name, runs_root=runs_root
        )
        failures.extend(diagnostics)
        # verified_total_mistakes is the number of these records, so a record
        # that counts one mistake twice inflates the learner's error rate and
        # nothing further down can tell. The step-d driver runs this check and
        # so does this module: a rule only the producer enforces is a rule that
        # holds until someone reruns the producer with a patched file in place.
        failures.extend(verify_records(records, {u.utterance_id: u.text for u in members}))
        verified_total += len(records)
        verified.update({record.record_id: record for record in records})

    shared: list[MistakeRecord] = []
    seen: set[str] = set()
    for path, _ in sessions:
        records, diagnostics = _records_for(
            run_id, StepId.E_VERIFIED, path.name, runs_root=runs_root
        )
        failures.extend(diagnostics)
        for record in records:
            if verified.get(record.record_id) != record:
                failures.append(
                    Diagnostic.from_code(
                        "SCHEMA_INVALID_VALUE",
                        "a step-e record is not one step d wrote, so what would be shared is "
                        "not a subset of what was verified",
                        item_ref=record.record_id,
                    )
                )
            elif record.record_id in seen:
                failures.append(
                    Diagnostic.from_code(
                        "CARDINALITY_MISMATCH",
                        "step e carries one record twice, so it would be shared and counted twice",
                        item_ref=record.record_id,
                    )
                )
            seen.add(record.record_id)
        shared.extend(records)
    if failures:
        _refuse(failures)

    analyzed = [u for u in corpus if u.utterance_id in processed]
    counts = AuditCounts(
        # english_words, not count_words. These fields are named
        # eligible_english_words and analyzed_english_words, they are the
        # denominator of every rate the review page shows and the submission
        # package carries, and step c records the English slice while
        # verify_corpus re-derives it the same way. Counting all words here made
        # the one number that leaves the machine the only one computed by a
        # different rule — larger than the verified corpus by however much
        # non-English the learner wrote, and larger in a way no check compared.
        eligible_english_words=sum(english_words(u.text) for u in corpus),
        analyzed_english_words=sum(english_words(u.text) for u in analyzed),
        eligible_utterances=len(corpus),
        analyzed_utterances=len(analyzed),
        written=_modality_counts(corpus, Modality.WRITTEN, analyzed_ids=processed),
        spoken_asr=_modality_counts(corpus, Modality.SPOKEN_ASR, analyzed_ids=processed),
        verified_total_mistakes=verified_total,
        shared_mistakes=len(shared),
        # Step e may only drop records, so every verified mistake is either
        # shared or was dropped there. Nothing can go missing between the two
        # steps, which is why no processing-failure class appears here.
        withheld_by_user=0,
        withheld_for_privacy=verified_total - len(shared),
        other_withheld={},
    )

    creator_version = _cleared_by(run_id, StepId.D_MISTAKES, runs_root=runs_root)
    verifier_version = _cleared_by(run_id, StepId.E_VERIFIED, runs_root=runs_root)
    artifact = ReviewedSubmissionArtifact(
        envelope=ArtifactEnvelope(
            schema_name="reviewed_submission",
            schema_version=1,
            artifact_id=new_artifact_id(),
            run_id=run_id,
            step_id=StepId.E_VERIFIED,
            producer_name=PRODUCER_NAME,
            producer_version=CLIENT_VERSION,
            created_at=utc_now(),
        ),
        records=[
            ReviewedRecord(
                mistake_id=record.record_id,
                record=record.shareable(),
                included=True,
                privacy_creator_version=creator_version,
                privacy_verifier_version=verifier_version,
            )
            for record in shared
        ],
        counts=counts,
    )
    target = ensure_private_dir(submission_dir(run_id, root=runs_root))
    write_model(target / REVIEWED_NAME, artifact)
    # The run is now waiting on a person rather than on a step. Recording that
    # is what lets a resumed run reopen the review instead of rebuilding it.
    enter_review(run_id, runs_root=runs_root)
    return artifact


def _report(artifact: ReviewedSubmissionArtifact, extra: Mapping[str, int]) -> str:
    counts = artifact.counts
    return (
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
                **extra,
            },
            indent=2,
        )
        + "\n"
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Build the reviewed submission artifact")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--runs-root", type=Path, default=None, help="test override")
    parser.add_argument(
        "--analyzed-from-steps",
        action="store_true",
        help="derive the analyzed set from which sessions step d actually read",
    )
    arguments = parser.parse_args(argv)
    analyzed = (
        analyzed_ids_from_steps(arguments.run_id, runs_root=arguments.runs_root)
        if arguments.analyzed_from_steps
        else None
    )
    artifact = build_review(arguments.run_id, analyzed_ids=analyzed, runs_root=arguments.runs_root)
    corpus_dir = step_dir(arguments.run_id, StepId.C_AUTHORED, root=arguments.runs_root)
    sys.stdout.write(
        _report(
            artifact,
            {
                # Items step c emptied: the learner wrote none of that message.
                # Absent from every count above, so saying the number out loud
                # is what keeps the denominator explainable.
                "non_authored_utterances": sum(
                    1 for _, members in read_all(corpus_dir) for u in members if not u.text.strip()
                ),
                "unjudged_utterances": _unjudged_utterances(
                    arguments.run_id, runs_root=arguments.runs_root
                ),
                "deduplicated_messages": _duplicates_removed(
                    arguments.run_id, runs_root=arguments.runs_root
                ),
            },
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

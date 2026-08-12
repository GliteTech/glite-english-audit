"""CLI: step d — one mistake file per session, clean when it is written.

Run ``uv run python -m glite_english_audit.pipeline.mistakes --run-id <id>
--prepare`` to name the work, then ``--apply`` once every agent has written its
file.

One session file is the unit of work. ``--prepare`` names the step-c file each
agent reads and the step-d file it must write back; ``--apply`` checks every one
of those files and promotes the step only when they all pass. Nothing is batched
any more: a batch pooled 25 utterances from wherever they came from, and the
pooling is what made "what did this step do to session X" unanswerable.

Step d is required to emit records that are already privacy-clean, with
synthetic examples, so the scanner runs here as a check rather than as a filter.
A scanner hit fails the file and says so instead of quietly dropping the record,
because a dropped record leaves a smaller count that nobody can explain and
turns a defect in the producing skill into a number the product publishes.
Step e should have nothing left to do.

The checks, all deterministic and with no model in this process:

- every line validates as :class:`MistakeRecord`;
- every evidence span lies inside the utterance it cites, in this session's own
  step-c file;
- no two records for one utterance cover overlapping spans;
- :func:`scan_safe_record` reports nothing.

The overlap rule decides a number the product publishes. Two records may sit
side by side in one sentence — a missing article and a wrong preposition are two
mistakes — but they cannot occupy the same characters. Overlap means one error
counted twice, and that count is the numerator of the learner's error rate.
"""

import argparse
import itertools
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from glite_english_audit import CLIENT_VERSION
from glite_english_audit.artifacts.enums import StepId, StepStatus
from glite_english_audit.artifacts.envelope import utc_now
from glite_english_audit.artifacts.hashing import new_artifact_id, sha256_hex
from glite_english_audit.artifacts.io import ensure_private_dir, write_jsonl_models, write_model
from glite_english_audit.artifacts.models import MistakeRecord
from glite_english_audit.consent import require_provider_transfer_consent
from glite_english_audit.diagnostics.codes import Diagnostic
from glite_english_audit.normalization.tokenizer import count_words
from glite_english_audit.paths import repo_root, step_dir
from glite_english_audit.pipeline.agent_budget import WorkItem, plan_step
from glite_english_audit.pipeline.agent_io import (
    MistakeDraft,
    agent_dir,
    decision_path,
    expand_mistakes,
    project_records,
    project_utterances,
    projection_path,
    verdict_path,
)
from glite_english_audit.pipeline.record_step import advance_to, mark_failed, output_is_current
from glite_english_audit.sessions import read_index, read_session, session_files, write_index
from glite_english_audit.state.run_store import load_manifest
from glite_english_audit.verification.privacy_scanner import scan_safe_record
from glite_english_audit.verification.reports import VerificationReport
from glite_english_audit.verification.skills import skill_versions

STEP = StepId.D_MISTAKES
SOURCE_STEP = StepId.C_AUTHORED
REPORT_NAME = "verification-report.json"
PRODUCER_NAME = "pipeline.mistakes"
SKILL_NAME = "find-english-mistakes"
QUARANTINE_DIR_NAME = "quarantined"


class SessionAssignment(BaseModel):
    """One session file an agent reads, and the file it must write back.

    ``items`` counts what is worth reading in the input: utterances the learner
    wrote text in for step d, records for step e. ``words`` is the learner's
    English in that session, which is what makes one assignment larger than
    another.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    read: str
    """The projection to read. Not the step-c or step-d file: those carry
    identity and provenance the judgment does not use."""
    write: str
    """Where to write the decisions. The driver expands them into the artifact,
    so an agent never writes a step file itself."""
    items: int = Field(ge=0)
    already_written: bool = False
    """True when an output file of this name exists and may be reused.

    False for a step the manifest has invalidated, whatever is on disk: a
    changed skill, prompt or model is why it was invalidated, so the answer
    sitting there is the one being replaced."""
    words: int = Field(ge=0)


class MistakesOutcome(BaseModel):
    """What ``--apply`` found, in counts and diagnostic codes only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    passed: bool
    sessions: int = Field(ge=0)
    records: int = Field(ge=0)
    sessions_with_records: int = Field(ge=0)
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    next_step: list[SessionAssignment] = Field(default_factory=list)


def _validation_diagnostic(error: ValidationError, reference: str) -> Diagnostic:
    """Name what is wrong with a line, so the fix does not need a guess."""
    kinds = {item["type"] for item in error.errors()}
    if "extra_forbidden" in kinds:
        return Diagnostic.from_code(
            "SCHEMA_UNEXPECTED_FIELD",
            "a record line carries a field the record model forbids",
            item_ref=reference,
        )
    if "missing" in kinds:
        return Diagnostic.from_code(
            "SCHEMA_MISSING_FIELD",
            "a record line is missing a required field",
            item_ref=reference,
        )
    return Diagnostic.from_code(
        "SCHEMA_INVALID_VALUE", "a record line fails record validation", item_ref=reference
    )


def read_lines[Line: BaseModel](
    path: Path, model: type[Line]
) -> tuple[list[Line], list[Diagnostic]]:
    """Parse one JSONL file into ``model``, reporting each bad line.

    A single malformed line must not hide the rest of the file: the agent
    repairing it needs every problem at once, not the first one.
    """
    items: list[Line] = []
    diagnostics: list[Diagnostic] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        reference = f"{path.name}:{number}"
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            diagnostics.append(
                Diagnostic.from_code(
                    "SCHEMA_INVALID_JSON", "a record line is not valid JSON", item_ref=reference
                )
            )
            continue
        try:
            items.append(model.model_validate(payload))
        except ValidationError as error:
            diagnostics.append(_validation_diagnostic(error, reference))
    return items, diagnostics


def read_records(path: Path) -> tuple[list[MistakeRecord], list[Diagnostic]]:
    """Parse one step-d or step-e artifact file."""
    return read_lines(path, MistakeRecord)


def read_drafts(path: Path) -> tuple[list[MistakeDraft], list[Diagnostic]]:
    """Parse one agent-written step-d decision file."""
    return read_lines(path, MistakeDraft)


def _overlap_diagnostics(records: Sequence[MistakeRecord]) -> list[Diagnostic]:
    """Two records for one utterance may not cover the same characters."""
    grouped: dict[str, list[MistakeRecord]] = {}
    for record in records:
        grouped.setdefault(record.utterance_id, []).append(record)
    diagnostics: list[Diagnostic] = []
    for group in grouped.values():
        ordered = sorted(
            group, key=lambda record: (record.evidence_span.start, record.evidence_span.end)
        )
        for earlier, later in itertools.pairwise(ordered):
            if earlier.evidence_span.end > later.evidence_span.start:
                diagnostics.append(
                    Diagnostic.from_code(
                        "CARDINALITY_MISMATCH",
                        "two records cover overlapping text, so one mistake is counted twice",
                        item_ref=later.record_id,
                    )
                )
    return diagnostics


def verify_records(
    records: Sequence[MistakeRecord], utterances: Mapping[str, str]
) -> list[Diagnostic]:
    """Check one session's records against the step-c text they cite.

    ``utterances`` is that session's own step-c file, keyed by utterance ID. The
    records carry no quoted text, so there is nothing to compare a quote
    against: the span addresses this text and the quote is resolved from it,
    which is why a fabricated quote is not something this function has to catch.
    """
    diagnostics: list[Diagnostic] = []
    for record in records:
        reference = record.record_id
        text = utterances.get(record.utterance_id)
        span = record.evidence_span
        if text is None:
            diagnostics.append(
                Diagnostic.from_code(
                    "LINEAGE_MISSING_INPUT",
                    "the record cites an utterance this session's step-c file does not contain",
                    item_ref=reference,
                )
            )
        elif span.end > len(text):
            diagnostics.append(
                Diagnostic.from_code(
                    "SCHEMA_INVALID_VALUE",
                    "the evidence span runs past the end of its utterance",
                    item_ref=reference,
                )
            )
        elif not text[span.start : span.end].strip():
            diagnostics.append(
                Diagnostic.from_code(
                    "SCHEMA_INVALID_VALUE",
                    "the evidence span covers no visible characters, so it quotes nothing",
                    item_ref=reference,
                )
            )
        # Step d owes clean records, so this is a defect report and never a
        # filter: the hit fails the file and the skill is fixed.
        diagnostics.extend(scan_safe_record(record.shareable(), item_ref=reference))
    diagnostics.extend(_overlap_diagnostics(records))
    return diagnostics


def step_digest(directory: Path) -> str:
    """One fingerprint for a step whose output is many files.

    The manifest records what was promoted and a report is about an artifact,
    so a per-session step still needs a single hash. Names are hashed with the
    bytes: a file that vanished must change the digest as surely as an edit.
    """
    lines = [f"{path.name}:{sha256_hex(path.read_bytes())}" for path in session_files(directory)]
    return sha256_hex("\n".join(lines).encode("utf-8"))


def _judged_by(run_id: str, skill_name: str, *, runs_root: Path | None = None) -> str:
    """The version of the skill whose judgment this report is about.

    The attestation this feeds claims which check cleared a record. It read
    ``CLIENT_VERSION``, which names whatever built the package — equally true of
    a run that skipped the check and one that passed it, so it distinguished
    nothing. The skill file is the check, so its declared version is the answer.

    Preferred source is the run manifest, which froze it at selection: that is
    the version resume compares, so a skill edited mid-run invalidates the step
    rather than being attested as if it had always been there. Runs started
    before the manifest recorded skill versions fall back to the version on
    disk, which for a run in progress is the same file the agents just read.

    There is deliberately no fall back to the client version. A missing
    attestation is recoverable; a wrong one is believed.

    Returned as the bare number, not ``name@version``. This value is copied into
    the submission package, whose gate refuses anything that is not a plain
    version — because a free-form version string is exactly the shape that
    carries a path, a session ID, or source text off the machine. Which skill it
    belongs to is fixed by the field: creator is step d, verifier is step e.
    """
    manifest = load_manifest(run_id, root=runs_root)
    version = manifest.fingerprint.skill_versions.get(skill_name)
    if version is None:
        version = skill_versions(repo_root()).get(skill_name)
    if version is None:
        msg = (
            f"no version is recorded for the {skill_name} skill, in the run manifest or on "
            "disk, so the attestation cannot say which check cleared these records"
        )
        raise ValueError(msg)
    return str(version)


def write_step_report(
    run_id: str,
    step: StepId,
    *,
    verifier_name: str,
    skill_name: str,
    artifact_id: str,
    artifact_hash: str,
    diagnostics: list[Diagnostic],
    runs_root: Path | None = None,
) -> VerificationReport:
    """Record what the check saw, beside the files it checked.

    Written whether or not the step passed. On a failure it is the list the
    repairing agent works from; on a pass it is the attestation the review reads
    the verifier version off, so a promoted step always names what cleared it.
    """
    report = VerificationReport(
        report_id=new_artifact_id(),
        run_id=run_id,
        step_id=step,
        artifact_id=artifact_id,
        artifact_hash=artifact_hash,
        verifier_name=verifier_name,
        verifier_version=_judged_by(run_id, skill_name, runs_root=runs_root),
        kind="deterministic",
        passed=VerificationReport.passed_matches(diagnostics),
        diagnostics=diagnostics,
        created_at=utc_now(),
    )
    # The directory may not exist yet when a step was never written into: the
    # report says so, and it must land owner-only rather than at the umask
    # default that an implicit mkdir would give it.
    target = ensure_private_dir(step_dir(run_id, step, root=runs_root))
    write_model(target / REPORT_NAME, report)
    return report


def prepare_mistakes(run_id: str, *, runs_root: Path | None = None) -> list[SessionAssignment]:
    """Create step d's directory and name the file each agent works on."""
    # A step-c file exists to be read by an agent, so preparing this step is the
    # moment the learner's sentences become provider-bound.
    require_provider_transfer_consent(run_id, runs_root=runs_root)
    source = step_dir(run_id, SOURCE_STEP, root=runs_root)
    inputs = session_files(source)
    if not inputs:
        msg = f"step c has no session files in {source.name}; run step c first"
        raise ValueError(msg)

    target = ensure_private_dir(step_dir(run_id, STEP, root=runs_root))
    # The index travels with the files, so every step keeps one mapping from
    # sequence number to session and no step has to re-derive it.
    write_index(target, read_index(source))

    # Step d could not resume at file granularity: an interrupted run re-asked
    # the agents for every session, including the ones already answered.
    ensure_private_dir(agent_dir(target))
    reusable = output_is_current(run_id, STEP, runs_root=runs_root)
    assignments: list[SessionAssignment] = []
    for path in inputs:
        utterances = read_session(path)
        # The projection numbers every utterance, including the ones step c
        # emptied: the agent's index has to address the same line the driver
        # will resolve its span against.
        projection = projection_path(target, path.name)
        write_jsonl_models(projection, project_utterances(utterances))
        decisions = decision_path(target, path.name)
        authored = [u for u in utterances if u.text.strip()]
        assignments.append(
            SessionAssignment(
                name=path.name,
                read=str(projection),
                write=str(decisions),
                items=len(authored),
                words=sum(count_words(u.text) for u in authored),
                already_written=reusable and decisions.is_file(),
            )
        )
    advance_to(run_id, STEP, StepStatus.IN_PROGRESS, runs_root=runs_root)
    return assignments


def _prepare_verification(
    run_id: str,
    records: Mapping[str, list[MistakeRecord]],
    words: Mapping[str, int],
    *,
    runs_root: Path | None = None,
) -> list[SessionAssignment]:
    """Create step e's directory and name its files.

    Step e has no prepare of its own because it has nothing left to decide, and
    promoting step d is the one moment that is both after the step-d files exist
    and before the step-e agents write into a directory that must be owner-only.
    """
    source = step_dir(run_id, STEP, root=runs_root)
    target = ensure_private_dir(step_dir(run_id, StepId.E_VERIFIED, root=runs_root))
    ensure_private_dir(agent_dir(target))
    write_index(target, read_index(source))
    assignments: list[SessionAssignment] = []
    for name, members in sorted(records.items()):
        # The published face and nothing else. The skill already told the agent
        # not to judge the utterance ID or the span because they are local
        # addresses; now it cannot see them, which is the same rule enforced
        # rather than asked for.
        projection = projection_path(target, name)
        write_jsonl_models(projection, project_records(members))
        assignments.append(
            SessionAssignment(
                name=name,
                read=str(projection),
                write=str(verdict_path(target, name)),
                items=len(members),
                words=words.get(name, 0),
            )
        )
    return assignments


def _quarantine(path: Path, step_directory: Path) -> None:
    """Move a rejected answer out of the way, keeping it readable.

    Kept rather than deleted for the same reason step c keeps one: a person
    asking why a session failed needs the answer that failed, and the diagnostic
    names a code rather than the text behind it.
    """
    if not path.is_file():
        return
    holding = ensure_private_dir(agent_dir(step_directory) / QUARANTINE_DIR_NAME)
    path.replace(holding / path.name)


def apply_mistakes(run_id: str, *, runs_root: Path | None = None) -> MistakesOutcome:
    """Validate every agent-written step-d file, then promote or refuse."""
    source = step_dir(run_id, SOURCE_STEP, root=runs_root)
    target = step_dir(run_id, STEP, root=runs_root)
    inputs = session_files(source)
    if not inputs:
        msg = f"step c has no session files in {source.name}; run step c first"
        raise ValueError(msg)

    diagnostics: list[Diagnostic] = []
    produced: dict[str, list[MistakeRecord]] = {}
    words: dict[str, int] = {}
    rejected: list[str] = []
    for path in inputs:
        utterances = read_session(path)
        words[path.name] = sum(count_words(u.text) for u in utterances)
        answers = decision_path(target, path.name)
        if not answers.is_file():
            diagnostics.append(
                Diagnostic.from_code(
                    "LINEAGE_MISSING_INPUT",
                    "step c has a session file step d did not answer; a session with no "
                    "mistakes is an empty file, not a missing one",
                    item_ref=path.name,
                )
            )
            continue
        drafts, parse_diagnostics = read_drafts(answers)
        records, expansion = expand_mistakes(utterances, drafts, item_ref=path.name)
        session_diagnostics = [
            *parse_diagnostics,
            *expansion,
            *verify_records(records, {u.utterance_id: u.text for u in utterances}),
        ]
        diagnostics.extend(session_diagnostics)
        if session_diagnostics:
            # The answer was rejected, so it must not be reported as already
            # answered when the step is prepared again: `prepare` decides that
            # by looking for the decision file, and a rejected answer left in
            # place makes the repair a pass that asks for nothing and reads the
            # same bad drafts back. The artifact is still written — the step
            # fails as a whole, so nothing downstream can read it, and a person
            # asking what went wrong needs the record beside the diagnostic.
            rejected.append(path.name)
        # The driver writes the artifact, so a record on disk is one that was
        # built from a verified draft rather than one the run has to trust.
        write_jsonl_models(target / path.name, records)
        produced[path.name] = records

    expected = {path.name for path in inputs}
    for path in session_files(target):
        if path.name not in expected:
            diagnostics.append(
                Diagnostic.from_code(
                    "CARDINALITY_MISMATCH",
                    "step d has a session file step c does not, so the two steps describe "
                    "different runs",
                    item_ref=path.name,
                )
            )

    artifact_id = new_artifact_id()
    report = write_step_report(
        run_id,
        STEP,
        verifier_name=PRODUCER_NAME,
        skill_name=SKILL_NAME,
        artifact_id=artifact_id,
        artifact_hash=step_digest(target),
        diagnostics=diagnostics,
        runs_root=runs_root,
    )
    total = sum(len(members) for members in produced.values())
    with_records = sum(1 for members in produced.values() if members)
    if not report.passed:
        # Quarantined rather than failed: the files exist and are the agents' to
        # repair, and the state machine lets a quarantined step re-enter work.
        for name in rejected:
            _quarantine(decision_path(target, name), target)
        mark_failed(run_id, STEP, quarantined=True, runs_root=runs_root)
        return MistakesOutcome(
            passed=False,
            sessions=len(inputs),
            records=total,
            sessions_with_records=with_records,
            diagnostics=diagnostics,
        )

    advance_to(
        run_id,
        STEP,
        StepStatus.PROMOTED,
        artifact_id=artifact_id,
        artifact_hash=report.artifact_hash,
        producer_version=CLIENT_VERSION,
        runs_root=runs_root,
    )
    return MistakesOutcome(
        passed=True,
        sessions=len(inputs),
        records=total,
        sessions_with_records=with_records,
        diagnostics=diagnostics,
        next_step=_prepare_verification(run_id, produced, words, runs_root=runs_root),
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Exits non-zero when a step-d file failed its check."""
    parser = argparse.ArgumentParser(description="Step d: one mistake file per session")
    parser.add_argument("--run-id", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--prepare",
        action="store_true",
        help="name the step-c file each agent reads and the step-d file it writes",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="validate every agent-written file and promote step d",
    )
    parser.add_argument("--runs-root", type=Path, default=None, help="test override")
    arguments = parser.parse_args(argv)

    if arguments.prepare:
        assignments = prepare_mistakes(arguments.run_id, runs_root=arguments.runs_root)
        # Planned over what is still outstanding: a resumed run must not be
        # packed for judgments it already holds.
        plan, _ = plan_step(
            [
                WorkItem(name=a.name, words=a.words, items=a.items)
                for a in assignments
                if not a.already_written
            ],
            step="d",
        )
        sys.stdout.write(
            json.dumps(
                {
                    "sessions": len(assignments),
                    "words": sum(assignment.words for assignment in assignments),
                    "plan": plan.model_dump(mode="json"),
                    "files": [assignment.model_dump(mode="json") for assignment in assignments],
                },
                indent=2,
            )
            + "\n"
        )
        return 0

    outcome = apply_mistakes(arguments.run_id, runs_root=arguments.runs_root)
    sys.stdout.write(json.dumps(outcome.model_dump(mode="json"), indent=2) + "\n")
    return 0 if outcome.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

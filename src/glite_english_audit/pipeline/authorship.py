"""CLI: step c — keep the learner's own words, one session file at a time.

Run::

    uv run python -m glite_english_audit.pipeline.authorship --run-id <id> --prepare
    uv run python -m glite_english_audit.pipeline.authorship --run-id <id> --apply

``--prepare`` names the step-b session files an agent must judge, one agent per
file, and creates the step-c directory with step b's session index copied
across. ``--apply`` verifies what the agents wrote and promotes the step.

The verification is the point of this module. A step-c file repeats its step-b
file item for item, in the same order, with ``text`` replaced by the spans the
learner actually wrote joined by a newline; an utterance that was entirely
someone else's text keeps its place with empty text. Every span must therefore
occur in the step-b text character for character, in order, and without
overlap, which one forward scan decides. A span the model repaired, translated,
or invented cannot be located, so it never becomes a counted word — and that
count is the denominator of every rate this product reports.

A file failing any check is quarantined whole: it moves out of the corpus into
``quarantined/``, none of its utterances are counted, and its name goes into
``needs-repair.json`` so ``--prepare --repair-only`` can ask for exactly those
sessions again. Quarantine is per file because the file is the unit of work;
there is no partial acceptance.

The step is promoted even when some sessions are quarantined, and the command
exits non-zero. The verified files are durable and the index counts what was
lost, so the run can continue after a bounded repair rather than restart, but
nothing may read the exit code as "all sessions judged".
"""

import argparse
import json
import sys
from pathlib import Path
from typing import NamedTuple

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from glite_english_audit import CLIENT_VERSION
from glite_english_audit.artifacts.enums import StageStatus, StepId
from glite_english_audit.artifacts.envelope import ArtifactEnvelope, utc_now
from glite_english_audit.artifacts.hashing import new_artifact_id, sha256_hex
from glite_english_audit.artifacts.io import ensure_private_dir, write_model
from glite_english_audit.artifacts.models import NormalizedUtterance
from glite_english_audit.consent import require_provider_transfer_consent
from glite_english_audit.diagnostics.codes import Diagnostic, Severity
from glite_english_audit.normalization.language import classify_english
from glite_english_audit.normalization.tokenizer import TOKENIZER_VERSION, count_words
from glite_english_audit.paths import step_dir
from glite_english_audit.pipeline.record_stage import advance_to, output_is_current
from glite_english_audit.sessions import read_all, read_index, session_files, write_index

INDEX_NAME = "authored-corpus-index.json"
REPAIR_NAME = "needs-repair.json"
QUARANTINE_DIR_NAME = "quarantined"
PRODUCER_NAME = "pipeline.authorship"
SCHEMA_NAME = "authored_corpus"


def english_words(text: str) -> int:
    """Words in ``text`` that count toward the English denominator.

    The file keeps what the learner wrote, in their own words and scripts. The
    count does not: it is the denominator of every rate this product reports,
    and a Russian sentence dictated between two English ones is not English the
    learner got wrong. ``classify_english`` returns the English slice, or
    nothing for text with too little English left to judge.

    Text and count deliberately disagree here, and that is the point. The old
    pipeline rewrote the utterance to its English slice, which made the count
    right by making the artifact a paraphrase of what was said. Keeping the
    text verbatim and narrowing only the count gives both an honest answer.
    """
    decision = classify_english(text)
    if decision.english_text is None:
        return 0
    return count_words(decision.english_text)


class PreparedSession(BaseModel):
    """One session file an agent must judge, and what it holds."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    file_name: str
    input_path: str
    output_path: str
    utterance_count: int = Field(ge=0)
    word_count: int = Field(ge=0)
    already_written: bool
    """True when a step-c file of this name exists.

    An interrupted run resumes by asking only for the sessions still missing,
    rather than paying for every judgment again."""


class PreparedStep(BaseModel):
    """What ``--prepare`` hands the orchestrator: file names and counts only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_dir: str
    output_dir: str
    repair_only: bool
    utterance_count: int = Field(ge=0)
    word_count: int = Field(ge=0)
    sessions: list[PreparedSession] = Field(default_factory=list)


class AuthoredSession(BaseModel):
    """One verified step-c file, hashed so a later reader can re-check it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    file_name: str
    utterance_count: int = Field(ge=0)
    word_count: int = Field(ge=0)
    sha256: str


class AuthoredCorpusIndex(BaseModel):
    """The step-c corpus: which session files count, and for how many words.

    There is no pooled corpus file to hash, so the index carries one hash per
    session file plus a digest over the set. Both are what
    :mod:`glite_english_audit.verification.verify_corpus` re-derives.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    envelope: ArtifactEnvelope
    tokenizer_version: str
    session_count: int = Field(ge=0)
    utterance_count: int = Field(ge=0)
    word_count: int = Field(ge=0)
    quarantined_session_count: int = Field(ge=0)
    quarantined_utterance_count: int = Field(ge=0)
    corpus_sha256: str
    sessions: list[AuthoredSession] = Field(default_factory=list)


class AuthorshipApplication(BaseModel):
    """What the agents returned and what the tokenizer counted, in numbers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    index: AuthoredCorpusIndex
    sessions_in: int = Field(ge=0)
    sessions_verified: int = Field(ge=0)
    sessions_quarantined: int = Field(ge=0)
    utterances_in: int = Field(ge=0)
    words_before: int = Field(ge=0)
    words_after: int = Field(ge=0)
    diagnostics: list[Diagnostic] = Field(default_factory=list)


def authored_dir(run_id: str, *, runs_root: Path | None = None) -> Path:
    """Directory the agents write one step-c session file into."""
    return step_dir(run_id, StepId.C_AUTHORED, root=runs_root)


def quarantine_dir(run_id: str, *, runs_root: Path | None = None) -> Path:
    """Where a step-c file that failed verification is kept.

    A subdirectory, so every reader listing session files sees the corpus and
    nothing else. The file is kept rather than deleted, which leaves the
    failure inspectable.
    """
    return authored_dir(run_id, runs_root=runs_root) / QUARANTINE_DIR_NAME


def repair_list_path(run_id: str, *, runs_root: Path | None = None) -> Path:
    """Where the session files still needing a judgment are listed."""
    return authored_dir(run_id, runs_root=runs_root) / REPAIR_NAME


def read_repair_list(run_id: str, *, runs_root: Path | None = None) -> list[str]:
    """The session files whose judgment failed and must be asked again.

    Specification 6.4 allows a bounded repair. Until something read this list
    there was no repair: a quarantined session was named in a file and then
    lost, and its words left the denominator with no explanation.
    """
    target = repair_list_path(run_id, runs_root=runs_root)
    if not target.is_file():
        return []
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except ValueError:
        return []
    items = payload.get("needs_repair", []) if isinstance(payload, dict) else []
    return [
        item["file_name"]
        for item in items
        if isinstance(item, dict) and isinstance(item.get("file_name"), str)
    ]


def _validation_code(error: ValidationError) -> str:
    """Name what the model rejected, so a repair pass knows what to fix."""
    types = {str(entry["type"]) for entry in error.errors()}
    if "missing" in types:
        return "SCHEMA_MISSING_FIELD"
    if "extra_forbidden" in types:
        return "SCHEMA_UNEXPECTED_FIELD"
    return "SCHEMA_INVALID_VALUE"


def read_authored(raw: bytes, *, item_ref: str) -> list[NormalizedUtterance] | Diagnostic:
    """Parse one agent-written step-c file, reporting the first bad line.

    Bad lines are diagnosed rather than raised: the file is model output, and a
    malformed one must quarantine its session instead of stopping the run.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return Diagnostic.from_code(
            "SCHEMA_INVALID_JSON", "a step-c session file is not valid UTF-8", item_ref=item_ref
        )
    items: list[NormalizedUtterance] = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        reference = f"{item_ref}:{number}"
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            return Diagnostic.from_code(
                "SCHEMA_INVALID_JSON", "a step-c line is not valid JSON", item_ref=reference
            )
        if not isinstance(payload, dict):
            return Diagnostic.from_code(
                "SCHEMA_INVALID_VALUE", "a step-c line is not a JSON object", item_ref=reference
            )
        try:
            items.append(NormalizedUtterance.model_validate(payload))
        except ValidationError as error:
            return Diagnostic.from_code(
                _validation_code(error),
                "a step-c line does not validate as an utterance",
                item_ref=reference,
            )
    return items


def span_diagnostic(authored: str, source: str, *, item_ref: str) -> Diagnostic | None:
    """Locate every retained span in ``source``, in order and without overlap.

    Splitting on the newline inverts the join the step performs, so the scan
    sees the spans the agent chose. A span found only behind the cursor is an
    overlap or a reordering; a span found nowhere was never the learner's.
    """
    cursor = 0
    for span in authored.split("\n"):
        position = source.find(span, cursor)
        if position >= 0:
            cursor = position + len(span)
            continue
        if span in source:
            return Diagnostic.from_code(
                "AUTHORSHIP_SPAN_ORDER_INVALID",
                "a retained span overlaps an earlier span or breaks their original order",
                item_ref=item_ref,
            )
        return Diagnostic.from_code(
            "AUTHORSHIP_SPAN_NOT_VERBATIM",
            "a retained span is not an exact substring of the step-b text",
            item_ref=item_ref,
        )
    return None


def verify_session(
    source: list[NormalizedUtterance],
    authored: list[NormalizedUtterance],
    *,
    item_ref: str,
) -> Diagnostic | None:
    """Return the diagnostic that quarantines this session file, or ``None``."""
    if len(authored) != len(source):
        return Diagnostic.from_code(
            "CARDINALITY_MISMATCH",
            f"step c returned {len(authored)} items for a session step b left with {len(source)}",
            item_ref=item_ref,
        )
    known = {utterance.utterance_id for utterance in source}
    seen: set[str] = set()
    for expected, actual in zip(source, authored, strict=True):
        if actual.utterance_id in seen:
            return Diagnostic.from_code(
                "AUTHORSHIP_DUPLICATE_DECISION",
                "more than one step-c item covers the same utterance",
                item_ref=item_ref,
            )
        seen.add(actual.utterance_id)
        if actual.utterance_id != expected.utterance_id:
            if actual.utterance_id not in known:
                return Diagnostic.from_code(
                    "AUTHORSHIP_UNKNOWN_UTTERANCE",
                    "a step-c item names an utterance this session does not contain",
                    item_ref=item_ref,
                )
            return Diagnostic.from_code(
                "CARDINALITY_MISMATCH",
                "step c does not repeat its step-b utterances in order",
                item_ref=item_ref,
            )
        # Only `text` may change. Everything else is provenance the adapters
        # established, and a rewritten timestamp or modality would travel
        # unchecked into every later step.
        if actual.model_dump(exclude={"text"}) != expected.model_dump(exclude={"text"}):
            return Diagnostic.from_code(
                "SCHEMA_INVALID_VALUE",
                "a step-c item changes a field other than its text",
                item_ref=item_ref,
            )
        diagnostic = span_diagnostic(actual.text, expected.text, item_ref=item_ref)
        if diagnostic is not None:
            return diagnostic
    return None


def prepare(
    run_id: str, *, runs_root: Path | None = None, repair_only: bool = False
) -> PreparedStep:
    """Name the step-b session files to judge and open the step-c directory.

    With ``repair_only``, name only the sessions whose verification failed, so
    a second pass re-asks for exactly those instead of redoing the run or
    accepting the loss.
    """
    # An agent reads these files, so preparing the step is the moment the
    # learner's sentences become provider-bound.
    require_provider_transfer_consent(run_id, runs_root=runs_root)
    source = step_dir(run_id, StepId.B_DEDUPLICATED, root=runs_root)
    per_file = list(read_all(source))
    if not per_file:
        msg = f"step b has no session files in {source.name}; run deduplicate first"
        raise ValueError(msg)

    target = ensure_private_dir(authored_dir(run_id, runs_root=runs_root))
    if repair_only:
        wanted = set(read_repair_list(run_id, runs_root=runs_root))
        per_file = [(path, members) for path, members in per_file if path.name in wanted]
        if not per_file:
            # An empty repair pass that returns zero is a pass that reports
            # success for work nobody did.
            msg = "no step-b session is listed for repair, so there is no repair pass to run"
            raise ValueError(msg)

    # An invalidated step's files are stale by definition: a changed skill,
    # prompt or model is exactly why it was invalidated, so the answer on disk
    # is the one being replaced.
    reusable = output_is_current(run_id, StepId.C_AUTHORED, runs_root=runs_root)
    sessions = [
        PreparedSession(
            file_name=path.name,
            input_path=str(path),
            output_path=str(target / path.name),
            utterance_count=len(members),
            # Every word, not only the English ones: this number says how much
            # text the agent has to read, which is what it costs. The English
            # denominator is counted after the judgment, by `english_words`.
            word_count=sum(count_words(utterance.text) for utterance in members),
            already_written=reusable and (target / path.name).is_file(),
        )
        for path, members in per_file
    ]
    # Same names in, same names out — and the sequence-to-session mapping
    # travels with them, or step c's file names mean nothing on their own.
    write_index(target, read_index(source))
    return PreparedStep(
        input_dir=str(source),
        output_dir=str(target),
        repair_only=repair_only,
        utterance_count=sum(entry.utterance_count for entry in sessions),
        word_count=sum(entry.word_count for entry in sessions),
        sessions=sessions,
    )


def corpus_digest(entries: list[AuthoredSession]) -> str:
    """One digest over the whole verified file set, for the run manifest.

    There is no pooled corpus file to hash, and the run manifest records one
    hash per stage. Hashing the ordered ``name hash`` listing gives that one
    number without pooling the sentences back into a file.
    """
    ordered = sorted(entries, key=lambda entry: entry.file_name)
    listing = "\n".join(f"{entry.file_name} {entry.sha256}" for entry in ordered)
    return sha256_hex(listing.encode("utf-8"))


def _quarantine(path: Path, *, run_id: str, runs_root: Path | None) -> None:
    """Move a failed step-c file out of the corpus, keeping it inspectable."""
    if not path.is_file():
        return
    holding = ensure_private_dir(quarantine_dir(run_id, runs_root=runs_root))
    path.replace(holding / path.name)


def _write_repair_list(
    run_id: str, failures: list[tuple[str, str]], *, runs_root: Path | None = None
) -> Path:
    """Record which session files need re-judging, and why.

    Names and diagnostic codes only, never text.
    """
    target = repair_list_path(run_id, runs_root=runs_root)
    ensure_private_dir(target.parent)
    items = [{"file_name": name, "code": code} for name, code in sorted(failures)]
    target.write_text(
        json.dumps({"needs_repair": items}, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return target


def apply_authorship(run_id: str, *, runs_root: Path | None = None) -> AuthorshipApplication:
    """Verify every agent-written step-c file against step b, then promote."""
    source_dir = step_dir(run_id, StepId.B_DEDUPLICATED, root=runs_root)
    out_dir = ensure_private_dir(authored_dir(run_id, runs_root=runs_root))
    per_file = list(read_all(source_dir))
    if not per_file:
        msg = f"step b has no session files in {source_dir.name}; run deduplicate first"
        raise ValueError(msg)

    diagnostics: list[Diagnostic] = []
    failures: list[tuple[str, str]] = []
    entries: list[AuthoredSession] = []
    quarantined_utterances = 0
    utterances_in = 0
    words_before = 0

    for path, expected in per_file:
        utterances_in += len(expected)
        # English words on both sides, so the before/after delta measures the
        # authorship judgment alone. Counting all words before and English
        # words after would fold two unrelated reductions into one number and
        # then report it as what the model removed.
        words_before += sum(english_words(utterance.text) for utterance in expected)
        target = out_dir / path.name
        outcome = _verify_one(target, expected)
        if isinstance(outcome, Diagnostic):
            diagnostics.append(outcome)
            failures.append((path.name, outcome.code))
            quarantined_utterances += len(expected)
            _quarantine(target, run_id=run_id, runs_root=runs_root)
            continue
        entries.append(
            AuthoredSession(
                file_name=target.name,
                utterance_count=len(outcome.items),
                word_count=sum(english_words(utterance.text) for utterance in outcome.items),
                sha256=sha256_hex(outcome.raw),
            )
        )
        # A session that passes this time is no longer waiting for repair, and
        # a stale copy under quarantine would say otherwise.
        (quarantine_dir(run_id, runs_root=runs_root) / target.name).unlink(missing_ok=True)

    # A stray file is quarantined but never listed for repair: no step-b
    # session asked for it, so there is nothing to ask again.
    expected_names = {path.name for path, _ in per_file}
    for stray in session_files(out_dir):
        if stray.name in expected_names:
            continue
        diagnostics.append(
            Diagnostic.from_code(
                "CARDINALITY_MISMATCH",
                "step c holds a session file step b never produced",
                item_ref=stray.name,
            )
        )
        _quarantine(stray, run_id=run_id, runs_root=runs_root)

    index = AuthoredCorpusIndex(
        envelope=ArtifactEnvelope(
            schema_name=SCHEMA_NAME,
            schema_version=1,
            artifact_id=new_artifact_id(),
            run_id=run_id,
            stage_id=StepId.C_AUTHORED,
            producer_name=PRODUCER_NAME,
            producer_version=CLIENT_VERSION,
            created_at=utc_now(),
        ),
        tokenizer_version=TOKENIZER_VERSION,
        session_count=len(entries),
        utterance_count=sum(entry.utterance_count for entry in entries),
        word_count=sum(entry.word_count for entry in entries),
        quarantined_session_count=len(per_file) - len(entries),
        quarantined_utterance_count=quarantined_utterances,
        corpus_sha256=corpus_digest(entries),
        sessions=entries,
    )
    write_model(out_dir / INDEX_NAME, index)
    _write_repair_list(run_id, failures, runs_root=runs_root)
    # The verified files are durable, so the manifest may point at them. Step c
    # is deterministic once the agents' files are in hand: the span scan above
    # is the check, and a file that fails it is quarantined rather than
    # corrected, so there is no second opinion left to wait for.
    advance_to(
        run_id,
        StepId.C_AUTHORED,
        StageStatus.PROMOTED,
        artifact_id=index.envelope.artifact_id,
        artifact_hash=index.corpus_sha256,
        producer_version=CLIENT_VERSION,
        runs_root=runs_root,
    )
    return AuthorshipApplication(
        index=index,
        sessions_in=len(per_file),
        sessions_verified=index.session_count,
        sessions_quarantined=index.quarantined_session_count,
        utterances_in=utterances_in,
        words_before=words_before,
        words_after=index.word_count,
        diagnostics=diagnostics,
    )


class _Verified(NamedTuple):
    """A step-c file that passed, with the bytes its hash is taken over."""

    raw: bytes
    items: list[NormalizedUtterance]


def _verify_one(target: Path, expected: list[NormalizedUtterance]) -> _Verified | Diagnostic:
    """Read and check one step-c file against the step-b session it answers."""
    if not target.is_file():
        return Diagnostic.from_code(
            "LINEAGE_MISSING_INPUT",
            "no step-c file was written for this session",
            item_ref=target.name,
        )
    raw = target.read_bytes()
    authored = read_authored(raw, item_ref=target.name)
    if isinstance(authored, Diagnostic):
        return authored
    diagnostic = verify_session(expected, authored, item_ref=target.name)
    if diagnostic is not None:
        return diagnostic
    return _Verified(raw, authored)


def _diagnostic_counts(diagnostics: list[Diagnostic]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for diagnostic in diagnostics:
        counts[diagnostic.code] = counts.get(diagnostic.code, 0) + 1
    return dict(sorted(counts.items()))


def main(argv: list[str] | None = None) -> int:
    """CLI entry point printing file names and aggregate counts as JSON."""
    parser = argparse.ArgumentParser(description="Step c: keep only what the learner wrote")
    parser.add_argument("--run-id", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare", action="store_true", help="name the session files to judge")
    mode.add_argument("--apply", action="store_true", help="verify the judged files and promote")
    parser.add_argument(
        "--repair-only",
        action="store_true",
        help="with --prepare, name only the session files whose verification failed",
    )
    parser.add_argument("--runs-root", type=Path, default=None, help="test override")
    arguments = parser.parse_args(argv)

    if arguments.prepare:
        prepared = prepare(
            arguments.run_id, runs_root=arguments.runs_root, repair_only=arguments.repair_only
        )
        sys.stdout.write(json.dumps(prepared.model_dump(mode="json"), indent=2) + "\n")
        return 0

    if arguments.repair_only:
        parser.error("--repair-only selects what to prepare; it has no meaning with --apply")
    result = apply_authorship(arguments.run_id, runs_root=arguments.runs_root)
    sys.stdout.write(
        json.dumps(
            {
                "sessions_in": result.sessions_in,
                "sessions_verified": result.sessions_verified,
                "sessions_quarantined": result.sessions_quarantined,
                "utterances_in": result.utterances_in,
                "utterances_out": result.index.utterance_count,
                "quarantined_utterances": result.index.quarantined_utterance_count,
                "words_before": result.words_before,
                "words_after": result.words_after,
                "tokenizer_version": result.index.tokenizer_version,
                "needs_repair": str(
                    repair_list_path(arguments.run_id, runs_root=arguments.runs_root)
                ),
                "diagnostic_codes": _diagnostic_counts(result.diagnostics),
            },
            indent=2,
        )
        + "\n"
    )
    return 1 if any(d.severity is Severity.ERROR for d in result.diagnostics) else 0


if __name__ == "__main__":
    raise SystemExit(main())

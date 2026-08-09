"""What an agent reads, what it writes, and the scripts between.

An agent should be handed what it needs to judge and asked for what it decided.
It was handed and asked for whole artifacts instead: a step-c file was 13 fields
per line of which the agent chose one, and ``pipeline/authorship.py`` already
rejected any line whose other twelve differed from step b — so the code asserted
they had to be copies and then made the model type them anyway. Measured on a
real run, 77% of every step-c file was bookkeeping, and step e retyped every
record it had been given to report that nothing was wrong.

The cost was the smaller half. Of what the step-c agents read, 15% was identity:
``session_hash`` and ``source_path_hash``, 64 hex characters each, on every
line. :mod:`glite_english_audit.sessions` justifies opaque ``session-0001.jsonl``
filenames on the grounds that session identity in a model's context "spends
privacy for nothing", and then the file contents handed it over. The projection
that used to strip those fields lived in ``pipeline/batches.py`` and was deleted
along with the batching it shared a module with.

So there are three shapes per step, not one:

- a **projection**, written by the driver, holding what the judgment needs;
- a **decision**, written by the agent, holding only what it decided;
- the **artifact**, written by the driver, unchanged from what every later step
  and every existing test already expects.

That last point is what makes this safe to land: the artifacts are
byte-identical, so ``verify_corpus``, ``build_review``, ``step_layout`` and the
whole test suite keep working as a regression proof rather than being rewritten
alongside the thing they are meant to check.

The index ``i`` is one-based and local to a session file. It replaces
``utterance_id``, which is 64 characters carrying a truncated session hash, and
it needs no anchor: a shifted index fails the span scan that already runs,
because utterance four's text is not a verbatim substring of utterance three.
"""

from collections.abc import Iterable, Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from glite_english_audit.artifacts.enums import ExampleType, Modality
from glite_english_audit.artifacts.models import (
    EvidenceSpan,
    MistakeRecord,
    NormalizedUtterance,
)
from glite_english_audit.diagnostics.codes import Diagnostic

AGENT_DIR_NAME = "agent"
PROJECTION_SUFFIX = ".in.jsonl"
DECISION_SUFFIX = ".out.jsonl"
VERDICT_SUFFIX = ".out.json"
"""Step e answers with one object for a whole session, not a line per record.

A different suffix, because a name is an instruction: an agent handed a
``.jsonl`` path writes one record per line, which is exactly the mistake the
drop list exists to make impossible.
"""


class UtteranceForJudgment(BaseModel):
    """One utterance as steps c and d are given it.

    Everything here changes a judgment; nothing here names anyone.

    ``modality`` is present because the dictation rules turn on it — a finding
    built on a single unstressed function word is the recognizer's error, not
    the speaker's. ``content_flags`` because the adapter's own paste heuristics
    are evidence about authorship that the text alone does not carry: dropping
    them left the step-c skill weighing ``possible_paste`` against a field it
    could no longer see.

    Absent, deliberately: the utterance ID, the session hash, the source path
    hash, the adapter and its version, the timestamp, the text status, the
    authorship confidence and basis, and the destination app. None of them
    changes what the learner wrote, and several of them identify the person who
    wrote it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    i: int = Field(ge=1)
    modality: Modality
    text: str
    content_flags: list[str] = Field(default_factory=list)


class RecordForConfidentiality(BaseModel):
    """One mistake record as step e is given it: the published face alone.

    The skill already told the agent not to judge ``utterance_id`` or
    ``evidence_span`` because they are local addresses that never leave the
    machine. Now it does not see them, which is a stronger way to say the same
    thing.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    i: int = Field(ge=1)
    mistake: str
    rule: str
    example: str
    example_type: ExampleType


class AuthoredLine(BaseModel):
    """Step c's answer for one utterance: the spans the learner wrote.

    ``text`` is the retained spans joined by a newline, exactly as the artifact
    carries it, so the forward scan that verifies them is unchanged. An
    utterance the learner wrote none of is ``""`` — present and empty, because a
    vanished item and an emptied one mean different things.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    i: int = Field(ge=1)
    text: str


class MistakeDraft(BaseModel):
    """Step d's answer for one mistake: what it judged, and where.

    Missing next to :class:`~glite_english_audit.artifacts.models.MistakeRecord`
    are ``utterance_id``, ``source_type`` and ``modality``. All three are copies
    of the utterance the span addresses, so the expander re-derives them and the
    skill no longer has to spell out a resolution the code can do.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    i: int = Field(ge=1)
    span: tuple[int, int]
    mistake: str
    rule: str
    example: str
    example_type: ExampleType


class DropList(BaseModel):
    """Step e's answer for a whole session: which records must not be shared.

    One object per file rather than one per record. Step e may only remove, and
    a file rebuilt from an index list cannot add, alter, repeat or reorder a
    record — the four failures its driver used to detect become unrepresentable.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    drop: list[int] = Field(default_factory=list)


def agent_dir(step_directory: Path) -> Path:
    """Where a step keeps what its agents read and wrote.

    A subdirectory, so the step directory itself holds only artifacts and a
    person opening it sees one kind of file. It is inside ``steps/``, which
    retention already sweeps, so nothing new has to be remembered when a run
    expires.
    """
    return step_directory / AGENT_DIR_NAME


def projection_path(step_directory: Path, session_file: str) -> Path:
    """The file an agent reads for one session."""
    return agent_dir(step_directory) / (_stem(session_file) + PROJECTION_SUFFIX)


def decision_path(step_directory: Path, session_file: str) -> Path:
    """The file an agent writes for one session, one answer per line."""
    return agent_dir(step_directory) / (_stem(session_file) + DECISION_SUFFIX)


def verdict_path(step_directory: Path, session_file: str) -> Path:
    """The file step e writes for one session: a single drop list."""
    return agent_dir(step_directory) / (_stem(session_file) + VERDICT_SUFFIX)


def _stem(session_file: str) -> str:
    return session_file.removesuffix(".jsonl")


def project_utterances(utterances: Iterable[NormalizedUtterance]) -> list[UtteranceForJudgment]:
    """What steps c and d hand an agent, in the order the session holds."""
    return [
        UtteranceForJudgment(
            i=index,
            modality=utterance.modality,
            text=utterance.text,
            content_flags=list(utterance.content_flags),
        )
        for index, utterance in enumerate(utterances, 1)
    ]


def project_records(records: Iterable[MistakeRecord]) -> list[RecordForConfidentiality]:
    """What step e hands an agent: the six shareable fields, minus the addresses."""
    return [
        RecordForConfidentiality(
            i=index,
            mistake=record.mistake,
            rule=record.rule,
            example=record.example,
            example_type=record.example_type,
        )
        for index, record in enumerate(records, 1)
    ]


def expand_authored(
    source: Sequence[NormalizedUtterance],
    decisions: Sequence[AuthoredLine],
    *,
    item_ref: str,
) -> tuple[list[NormalizedUtterance], list[Diagnostic]]:
    """Rebuild step c's artifact from step b plus the text the agent kept.

    Every index from 1 to ``len(source)`` must appear exactly once. Step c's
    file answers step b line for line, so a missing index is a question nobody
    answered and a repeated one is two answers to the same question; neither can
    be resolved by guessing.

    The two authorship codes carry over from when the agent named utterances
    directly. An index past the end still names an utterance this session does
    not contain, and a repeated index is still more than one decision covering
    one utterance — the same two failures, addressed a shorter way.
    """
    indices = [line.i for line in decisions]
    diagnostics = _out_of_range(indices, len(source), "AUTHORSHIP_UNKNOWN_UTTERANCE", item_ref)
    diagnostics.extend(_duplicates(indices, "AUTHORSHIP_DUPLICATE_DECISION", item_ref))
    if not diagnostics and len(indices) != len(source):
        diagnostics.append(
            Diagnostic.from_code(
                "CARDINALITY_MISMATCH",
                f"step c answered {len(indices)} of the {len(source)} items in this session; "
                "every item is answered exactly once, an unauthored one with empty text",
                item_ref=item_ref,
            )
        )
    if diagnostics:
        return [], diagnostics
    by_index = {line.i: line.text for line in decisions}
    expanded = [
        utterance.model_copy(update={"text": by_index[index]})
        for index, utterance in enumerate(source, 1)
    ]
    return expanded, []


def expand_mistakes(
    source: Sequence[NormalizedUtterance],
    drafts: Sequence[MistakeDraft],
    *,
    item_ref: str,
) -> tuple[list[MistakeRecord], list[Diagnostic]]:
    """Rebuild step d's artifact from the step-c utterances plus what was judged.

    Unlike step c this requires no coverage: a session with no mistakes is an
    empty file, and most utterances hold none. Indices may repeat — two records
    may cite one utterance — and whether their spans collide is the overlap
    check's question, not this one's.
    """
    diagnostics = _out_of_range(
        [draft.i for draft in drafts], len(source), "LINEAGE_MISSING_INPUT", item_ref
    )
    if diagnostics:
        return [], diagnostics

    expanded: list[MistakeRecord] = []
    for draft in drafts:
        utterance = source[draft.i - 1]
        start, end = draft.span
        try:
            span = EvidenceSpan(start=start, end=end)
        except ValueError:
            diagnostics.append(
                Diagnostic.from_code(
                    "SCHEMA_INVALID_VALUE",
                    "a draft span is not a half-open range with end after start",
                    item_ref=f"{item_ref}:{draft.i}",
                )
            )
            continue
        expanded.append(
            MistakeRecord(
                utterance_id=utterance.utterance_id,
                evidence_span=span,
                mistake=draft.mistake,
                rule=draft.rule,
                example=draft.example,
                example_type=draft.example_type,
                # Both re-derived from the utterance the span addresses. The
                # skill used to spell this resolution out in prose, which made
                # each of them something a model could get wrong.
                source_type=utterance.source_adapter,
                modality=(
                    Modality.SPOKEN_ASR
                    if utterance.modality is Modality.SPOKEN_ASR
                    else Modality.WRITTEN
                ),
            )
        )
    return expanded, diagnostics


def expand_verified(
    produced: Sequence[MistakeRecord], dropped: DropList, *, item_ref: str
) -> tuple[list[MistakeRecord], list[Diagnostic]]:
    """Rebuild step e's artifact as step d's records minus the dropped indices.

    The records are copied, never rebuilt from anything the agent wrote, so a
    step-e file that altered or invented a record is not something to detect —
    it is something that cannot be expressed.
    """
    diagnostics = _out_of_range(dropped.drop, len(produced), "CARDINALITY_MISMATCH", item_ref)
    diagnostics.extend(_duplicates(dropped.drop, "CARDINALITY_MISMATCH", item_ref))
    if diagnostics:
        return [], diagnostics
    withheld = set(dropped.drop)
    return [record for index, record in enumerate(produced, 1) if index not in withheld], []


def _out_of_range(
    indices: Sequence[int], available: int, code: str, item_ref: str
) -> list[Diagnostic]:
    """Diagnostics for answers addressing a line the session does not have."""
    return [
        Diagnostic.from_code(
            code,
            f"an answer addresses item {index} of a session holding {available}",
            item_ref=f"{item_ref}:{index}",
        )
        for index in sorted({index for index in indices if index > available})
    ]


def _duplicates(indices: Sequence[int], code: str, item_ref: str) -> list[Diagnostic]:
    """Diagnostics for an item answered more than once."""
    seen: set[int] = set()
    repeated: set[int] = set()
    for index in indices:
        if index in seen:
            repeated.add(index)
        seen.add(index)
    return [
        Diagnostic.from_code(
            code, f"item {index} is answered more than once", item_ref=f"{item_ref}:{index}"
        )
        for index in sorted(repeated)
    ]

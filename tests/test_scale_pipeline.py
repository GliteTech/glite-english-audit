"""Scale gate (specification, 13.8): 1M eligible words, 50k utterances — as files.

The gate used to be a number the deterministic functions could reach in memory.
It is a file-layout question now: one session is one file, so a real corpus is
thousands of files that every step after a has to read, judge and write back
under the same names. Session size on real data spans a factor of fifty —
someone dictates one sentence into a scratch window and then works a whole
afternoon in one agent session — so the run below is built with that spread and
with plenty of one-message sessions, which are the ones a batching shortcut
would quietly merge away.

Marked ``slow``: the full gate runs it, developers deselect it with
``-m "not slow"``.

Step a is the one driver this test stands in for. It is the adapters reading
their own storage formats, which the adapter tests cover file by file; what has
to hold up here is everything downstream of it, so the run starts from a step-a
directory written the way collect writes one, and steps b to e are the real
drivers with only their agents faked.
"""

import json
import resource
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

import pytest

from glite_english_audit.artifacts.enums import (
    AgentRuntime,
    ExampleType,
    Modality,
    OsEnvironment,
    RunStatus,
    StepId,
    StepStatus,
    TextStatus,
)
from glite_english_audit.artifacts.io import ensure_private_dir, write_jsonl_models, write_model
from glite_english_audit.artifacts.manifest import (
    MANIFEST_SCHEMA_VERSION,
    CompatibilityFingerprint,
    ConsentState,
    RunManifest,
    empty_step_map,
)
from glite_english_audit.artifacts.models import (
    EvidenceSpan,
    MistakeRecord,
    NormalizedUtterance,
    ReviewedSubmissionArtifact,
)
from glite_english_audit.consent import CONSENT_POLICY_VERSION
from glite_english_audit.normalization.tokenizer import TOKENIZER_VERSION, count_words
from glite_english_audit.paths import step_dir
from glite_english_audit.pipeline import authorship, build_review, deduplicate, mistakes, verify
from glite_english_audit.pipeline.deduplicate import REMOVED_NAME
from glite_english_audit.pipeline.record_step import advance_to
from glite_english_audit.sessions import (
    read_all,
    read_index,
    read_session,
    session_file_name,
    session_files,
    write_index,
)
from glite_english_audit.state.run_store import RUN_MANIFEST_FILENAME

pytestmark = pytest.mark.slow

_RUN = "run-" + "5" * 32
_NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)

_SESSIONS = 2_000
_LARGEST_SESSION = 50  # one message to fifty: the spread real data shows
_TIME_BUDGET_SECONDS = 300
_MEMORY_BUDGET_BYTES = 1_073_741_824

_SENTENCES = (
    "Yesterday I tried to explain the new caching idea to my colleague and it went fine",
    "Please check the second draft because I am not sure the wording sounds natural enough",
    "We should probably move the meeting to Thursday since everyone is busy this afternoon",
    "I very like this approach but maybe we can simplify the first part a little",
    "The deployment finished without errors so I will continue with the documentation now",
)
_TAIL = " number {order} and then some more words follow here"
_FLAGGED_PHRASE = "very like"

# One session in a hundred is given a mistake, which is about what real data
# produces and is what makes most step-d files empty at this scale.
_SESSIONS_WITH_A_MISTAKE = 100
# One utterance in ten comes back as a partial retention, so the denominator is
# not simply the input count.
_PARTIAL_RETENTION = 10


def _session_sizes() -> list[int]:
    """Sizes 1 to 50, every one of them used, in a fixed scattered order."""
    return [1 + (index * 7) % _LARGEST_SESSION for index in range(_SESSIONS)]


def _utterance(session: int, position: int, order: int) -> NormalizedUtterance:
    text = _SENTENCES[(session + position) % len(_SENTENCES)] + _TAIL.format(order=order)
    return NormalizedUtterance(
        utterance_id=f"scale-{order:06d}",
        source_adapter="claude_code",
        adapter_version="1.0.0",
        session_hash=f"{session:064x}",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=order),
        text=text,
        modality=Modality.WRITTEN,
        text_status=TextStatus.VERBATIM,
        authorship_confidence=0.9,
        authorship_basis="explicit_user_role",
        source_path_hash="a" * 64,
    )


def _pasted(source: list[NormalizedUtterance], session: int) -> list[NormalizedUtterance]:
    """One session that is nothing but another session's messages, again.

    Same words, a second application, and no usable timestamp — the same
    production event recorded twice. Deduplication has to collapse every one of
    them against an original that lives in a different file, and leave this
    session behind as an empty file rather than dropping it.
    """
    return [
        utterance.model_copy(
            update={
                "utterance_id": f"scale-copy-{index:06d}",
                "session_hash": f"{session:064x}",
                "source_adapter": "cursor",
                "source_path_hash": "b" * 64,
                "timestamp": None,
            }
        )
        for index, utterance in enumerate(source, start=1)
    ]


def _write_manifest(runs_root: Path) -> None:
    manifest = RunManifest(
        manifest_schema_version=MANIFEST_SCHEMA_VERSION,
        run_id=_RUN,
        created_at=_NOW,
        runtime=AgentRuntime.CLAUDE_CODE,
        os_environment=OsEnvironment.MACOS,
        status=RunStatus.AWAITING_PREFLIGHT,
        consent=ConsentState(
            consent_policy_version=CONSENT_POLICY_VERSION,
            local_scan_confirmed_at=_NOW,
            provider_transfer_confirmed_at=_NOW,
        ),
        steps=empty_step_map(),
        fingerprint=CompatibilityFingerprint(
            adapter_versions={},
            artifact_schema_version=MANIFEST_SCHEMA_VERSION,
            tokenizer_version=TOKENIZER_VERSION,
            skill_versions={},
            prompt_versions={},
            model_ids={},
            consent_policy_version=CONSENT_POLICY_VERSION,
        ),
    )
    write_model(ensure_private_dir(runs_root / _RUN) / RUN_MANIFEST_FILENAME, manifest)


def _write_step_a(runs_root: Path) -> tuple[int, str]:
    """A step-a directory of the shape and size collect would produce.

    Returns the utterance count and the name of the all-pasted session.
    """
    directory = ensure_private_dir(step_dir(_RUN, StepId.A_COLLECTED, root=runs_root))
    index: dict[str, str] = {}
    order = 0
    total = 0
    duplicated: list[NormalizedUtterance] = []
    for session, size in enumerate(_session_sizes(), start=1):
        members: list[NormalizedUtterance] = []
        for position in range(size):
            order += 1
            members.append(_utterance(session, position, order))
        if size == _LARGEST_SESSION and not duplicated:
            # The longest session, so the paste below is a whole afternoon's
            # dictation rather than one stray sentence.
            duplicated = members
        name = session_file_name(session)
        write_jsonl_models(directory / name, members)
        index[name] = f"{session:064x}"
        total += size

    # Last, because collect orders sessions by their earliest message and these
    # carry no timestamp at all.
    copies = _pasted(duplicated, _SESSIONS + 1)
    pasted_name = session_file_name(_SESSIONS + 1)
    write_jsonl_models(directory / pasted_name, copies)
    index[pasted_name] = f"{_SESSIONS + 1:064x}"
    write_index(directory, index)
    # Step a is stood in for, so its promotion is too: every later driver reads
    # the manifest, and build_review refuses a run that is not promoted through.
    advance_to(_RUN, StepId.A_COLLECTED, StepStatus.PROMOTED, runs_root=runs_root)
    return total + len(copies), pasted_name


def _authored_text(text: str, position: int) -> str:
    """The step-c agent: keep everything, or a verbatim prefix of it."""
    if position % _PARTIAL_RETENTION:
        return text
    return text.split(" number ")[0]


def _write_step_c(prepared: authorship.PreparedStep) -> None:
    position = 0
    for session in prepared.sessions:
        judged: list[NormalizedUtterance] = []
        for utterance in read_session(Path(session.input_path)):
            position += 1
            judged.append(
                utterance.model_copy(update={"text": _authored_text(utterance.text, position)})
            )
        write_jsonl_models(Path(session.output_path), judged)


def _record_for(utterance: NormalizedUtterance) -> MistakeRecord | None:
    start = utterance.text.find(_FLAGGED_PHRASE)
    if start < 0:
        return None
    return MistakeRecord(
        utterance_id=utterance.utterance_id,
        evidence_span=EvidenceSpan(start=start, end=start + len(_FLAGGED_PHRASE)),
        mistake="Used 'very' to modify the verb 'like'.",
        rule="In English, 'very' cannot modify a verb; use 'really' instead.",
        example="I really like this approach.",
        example_type=ExampleType.SYNTHETIC,
        source_type="claude_code",
        modality=Modality.WRITTEN,
    )


def _lines(path: Path) -> int:
    return len([line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()])


class _ScaleRun(NamedTuple):
    """One large run on disk, with what each driver reported about it."""

    runs_root: Path
    utterances: int
    pasted_name: str
    deduplicated: dict[str, object]
    prepared_c: authorship.PreparedStep
    authored: authorship.AuthorshipApplication
    produced: mistakes.MistakesOutcome
    verified: verify.VerificationOutcome
    reviewed: ReviewedSubmissionArtifact
    elapsed_seconds: float
    peak_bytes: int

    def step(self, step: StepId) -> Path:
        return step_dir(_RUN, step, root=self.runs_root)


@pytest.fixture(scope="module")
def scaled(tmp_path_factory: pytest.TempPathFactory) -> _ScaleRun:
    """The whole run at gate scale, built once for every test below."""
    runs_root = tmp_path_factory.mktemp("scale") / "runs"
    started = time.monotonic()
    _write_manifest(runs_root)
    utterances, pasted_name = _write_step_a(runs_root)

    deduplicated = deduplicate.deduplicate(_RUN, runs_root=runs_root)

    prepared_c = authorship.prepare(_RUN, runs_root=runs_root)
    _write_step_c(prepared_c)
    authored = authorship.apply_authorship(_RUN, runs_root=runs_root)

    assigned_d = mistakes.prepare_mistakes(_RUN, runs_root=runs_root)
    # Long enough to hold the flagged sentence at all, then one in a hundred of
    # those. The assignment's own item count is what decides, which is the
    # number an orchestrator sees before handing the file to an agent.
    long_enough = [entry for entry in assigned_d if entry.items >= len(_SENTENCES)]
    flagged = {entry.name for entry in long_enough[::_SESSIONS_WITH_A_MISTAKE]}
    for assignment in assigned_d:
        records: list[MistakeRecord] = []
        if assignment.name in flagged:
            for utterance in read_session(Path(assignment.read)):
                record = _record_for(utterance)
                if record is not None:
                    records.append(record)
                    break
        write_jsonl_models(Path(assignment.write), records)
    produced = mistakes.apply_mistakes(_RUN, runs_root=runs_root)

    dropped = False
    for assignment in produced.next_step:
        confirmed, _ = mistakes.read_records(Path(assignment.read))
        if confirmed and not dropped:
            confirmed = confirmed[1:]
            dropped = True
        write_jsonl_models(Path(assignment.write), confirmed)
    verified = verify.apply_verification(_RUN, runs_root=runs_root)

    reviewed = build_review.build_review(_RUN, runs_root=runs_root)
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return _ScaleRun(
        runs_root=runs_root,
        utterances=utterances,
        pasted_name=pasted_name,
        deduplicated=deduplicated,
        prepared_c=prepared_c,
        authored=authored,
        produced=produced,
        verified=verified,
        reviewed=reviewed,
        elapsed_seconds=time.monotonic() - started,
        peak_bytes=peak if sys.platform == "darwin" else peak * 1024,
    )


def test_the_run_is_the_size_the_gate_asks_for(scaled: _ScaleRun) -> None:
    # Everything below is only evidence about scale if the corpus is that
    # large, and the numbers come from the generator rather than the drivers.
    assert scaled.utterances >= 50_000
    assert scaled.reviewed.counts.eligible_english_words >= 1_000_000
    assert len(session_files(scaled.step(StepId.A_COLLECTED))) == _SESSIONS + 1


def test_every_session_keeps_its_file_and_its_name_through_every_step(scaled: _ScaleRun) -> None:
    names = [path.name for path in session_files(scaled.step(StepId.A_COLLECTED))]
    for step in StepId:
        assert [path.name for path in session_files(scaled.step(step))] == names
        assert read_index(scaled.step(step)) == read_index(scaled.step(StepId.A_COLLECTED))


def test_session_size_spans_a_factor_of_fifty_and_the_smallest_survive(
    scaled: _ScaleRun,
) -> None:
    """A one-message session is a session, not a rounding error.

    Half the value of one file per session is that the small ones stay
    addressable. A step that batched its input for efficiency would still
    produce the right totals here, and would lose exactly these.
    """
    collected = {
        path.name: len(members) for path, members in read_all(scaled.step(StepId.A_COLLECTED))
    }
    single = [name for name, size in collected.items() if size == 1]
    assert len(single) >= 20
    assert max(collected.values()) == _LARGEST_SESSION * min(collected.values())

    # Step c returns every item it was given, file by file, at this scale too.
    # Its input is step b, which is where the pasted session lost its messages.
    deduplicated = {
        path.name: len(members) for path, members in read_all(scaled.step(StepId.B_DEDUPLICATED))
    }
    authored = {
        path.name: len(members) for path, members in read_all(scaled.step(StepId.C_AUTHORED))
    }
    assert authored == deduplicated
    assert {
        entry.file_name: entry.utterance_count for entry in scaled.prepared_c.sessions
    } == deduplicated
    assert len(scaled.prepared_c.sessions) == len(collected), "every file gets its own agent"


def test_the_pasted_session_is_emptied_by_the_global_pass_and_kept(scaled: _ScaleRun) -> None:
    """The one case a per-file pass cannot see, among two thousand files.

    Every message in this session repeats one from another session, so all of
    them go and the file stays behind empty. Both halves matter: a per-file
    pass would keep the copies and inflate the denominator, and a step that
    deleted the emptied file would lose the fact that the session was read.
    """
    removed = json.loads(
        (scaled.step(StepId.B_DEDUPLICATED) / REMOVED_NAME).read_text(encoding="utf-8")
    )["removed"]
    assert list(removed) == [scaled.pasted_name]
    assert scaled.deduplicated["sessions_affected"] == 1
    assert scaled.deduplicated["removed"] == len(removed[scaled.pasted_name])
    for step in (StepId.B_DEDUPLICATED, StepId.C_AUTHORED, StepId.D_MISTAKES, StepId.E_VERIFIED):
        path = scaled.step(step) / scaled.pasted_name
        assert path.is_file()
        assert path.stat().st_size == 0

    # The originals stay, in the files they were always in, and nothing else
    # was collapsed on the way: every other session repeats sentence templates
    # that a looser rule would read as the same message twice.
    surviving = {
        utterance.utterance_id
        for _, members in read_all(scaled.step(StepId.B_DEDUPLICATED))
        for utterance in members
    }
    assert not any(identifier.startswith("scale-copy-") for identifier in surviving)
    assert len(surviving) == scaled.utterances - len(removed[scaled.pasted_name])
    assert scaled.deduplicated["messages_out"] == len(surviving)


def test_the_denominator_is_a_direct_count_over_the_step_c_files(scaled: _ScaleRun) -> None:
    """Two thousand files, one number, and it has to come from the files.

    The count the review publishes is assembled from every step-c file in the
    run. Recomputing it here from the same files is the only way to see that a
    file was not skipped: a run missing one session of fifty messages is still
    internally consistent and still reports a rate over a corpus the person did
    not have.
    """
    kept = [
        utterance
        for _, members in read_all(scaled.step(StepId.C_AUTHORED))
        for utterance in members
        if utterance.text.strip()
    ]
    direct = sum(count_words(utterance.text) for utterance in kept)
    assert scaled.reviewed.counts.eligible_english_words == direct
    assert scaled.reviewed.counts.eligible_utterances == len(kept)
    assert scaled.authored.index.word_count == direct
    assert scaled.authored.sessions_quarantined == 0
    assert scaled.authored.diagnostics == []

    # Smaller than the input, because the duplicates went and one utterance in
    # ten came back as a partial retention.
    before = sum(
        count_words(utterance.text)
        for _, members in read_all(scaled.step(StepId.B_DEDUPLICATED))
        for utterance in members
    )
    assert 0 < direct < before


def test_the_counts_reconcile_with_the_records_on_disk(scaled: _ScaleRun) -> None:
    counts = scaled.reviewed.counts
    in_d = sum(_lines(path) for path in session_files(scaled.step(StepId.D_MISTAKES)))
    in_e = sum(_lines(path) for path in session_files(scaled.step(StepId.E_VERIFIED)))
    assert in_d > in_e > 0
    assert counts.verified_total_mistakes == in_d
    assert counts.shared_mistakes == in_e == len(scaled.reviewed.records)
    assert counts.withheld_for_privacy == in_d - in_e
    assert scaled.verified.records_dropped == in_d - in_e
    assert scaled.produced.passed and scaled.verified.passed

    # Most sessions have nothing to report, and every one of them still answers
    # with a file.
    empty = [
        path for path in session_files(scaled.step(StepId.D_MISTAKES)) if path.stat().st_size == 0
    ]
    assert len(empty) == scaled.produced.sessions - scaled.produced.sessions_with_records
    assert len(empty) > _SESSIONS // 2


def test_the_run_stays_inside_its_time_and_memory_budget(scaled: _ScaleRun) -> None:
    # One file per session multiplies file handles, not resident text: nothing
    # here may hold the whole corpus twice, and a run of this size must still
    # finish in the time a person will wait for.
    assert scaled.elapsed_seconds < _TIME_BUDGET_SECONDS, (
        f"the per-file run took {scaled.elapsed_seconds:.0f}s"
    )
    assert scaled.peak_bytes < _MEMORY_BUDGET_BYTES, (
        f"peak RSS {scaled.peak_bytes / 1e6:.0f} MB exceeds 1 GiB"
    )

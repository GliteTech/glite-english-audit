"""Step c with an agent in the loop: session files out, judged session files in.

The agent picks which spans of each utterance the learner actually wrote; this
module's verifier decides whether to believe it. That verifier is what most of
these tests are about. A span it accepts becomes counted English, so a
paraphrase it lets through puts words the learner never wrote into the
denominator of every rate this product reports, and nothing downstream can tell
the difference afterwards.

The unit of acceptance is the file, not the utterance. So each failure below is
checked for what it does to the whole session: the file leaves the corpus, its
words leave the count, and its name goes on the list of sessions to ask again.
"""

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from glite_english_audit.artifacts.enums import (
    AgentRuntime,
    Modality,
    OsEnvironment,
    RunStatus,
    StepId,
    StepStatus,
    TextStatus,
)
from glite_english_audit.artifacts.io import write_jsonl_models, write_model
from glite_english_audit.artifacts.manifest import (
    MANIFEST_SCHEMA_VERSION,
    CompatibilityFingerprint,
    ConsentState,
    RunManifest,
    empty_step_map,
)
from glite_english_audit.artifacts.models import NormalizedUtterance
from glite_english_audit.consent import CONSENT_POLICY_VERSION, MissingConsentError
from glite_english_audit.diagnostics.codes import Diagnostic
from glite_english_audit.normalization.tokenizer import TOKENIZER_VERSION, count_words
from glite_english_audit.paths import step_dir
from glite_english_audit.pipeline import authorship
from glite_english_audit.pipeline.authorship import (
    QUARANTINE_DIR_NAME,
    AuthoredSession,
    corpus_digest,
    english_words,
    read_authored,
    span_diagnostic,
    verify_session,
)
from glite_english_audit.sessions import read_index, read_session
from glite_english_audit.sessions import write_index as write_session_index
from glite_english_audit.state.run_store import RUN_MANIFEST_FILENAME, RunStoreError, load_manifest
from glite_english_audit.verification.verify_corpus import verify_corpus

_RUN = "run-" + "2" * 32

_PLAIN = "I am agree that the second variant reads better."
_THANKS = _PLAIN + " Thanks."
_MIXED = "fix those issues\n$ npm run lint\napp.ts:14:3 error 'cfg' is assigned but never used"
_KEPT = "fix those issues"
_PASTED = "```python\nprint('hello')\n```"
_RERUN = "please rerun the failing test once more"
_RUSSIAN = "нужно починить деплой скрипт сегодня"

_SESSION_HASHES = {
    "session-0001.jsonl": "a" * 64,
    "session-0002.jsonl": "d" * 64,
    "session-0003.jsonl": "e" * 64,
}

# What sessions 1 and 2 contribute once judged: the prose and the one line of
# the mixed utterance the learner typed. Written with `count_words` so the
# expectation does not come from the function under test.
_KEPT_WORDS = count_words(_PLAIN) + count_words(_KEPT)


def _utterance(index: int, text: str, *, session: str = "a" * 64) -> NormalizedUtterance:
    return NormalizedUtterance(
        utterance_id=f"u-{index:03d}",
        source_adapter="claude_code",
        adapter_version="1.0.0",
        session_hash=session,
        timestamp=datetime(2026, 8, 1, 12, index, tzinfo=UTC),
        text=text,
        modality=Modality.WRITTEN,
        text_status=TextStatus.VERBATIM,
        authorship_confidence=0.9,
        authorship_basis="explicit_user_role",
        source_path_hash="c" * 64,
    )


def _write_manifest(runs_root: Path, *, provider_transfer: bool) -> None:
    moment = datetime(2026, 8, 1, tzinfo=UTC)
    manifest = RunManifest(
        manifest_schema_version=MANIFEST_SCHEMA_VERSION,
        run_id=_RUN,
        created_at=moment,
        runtime=AgentRuntime.CLAUDE_CODE,
        os_environment=OsEnvironment.MACOS,
        status=RunStatus.PROCESSING,
        consent=ConsentState(
            consent_policy_version=CONSENT_POLICY_VERSION,
            local_scan_confirmed_at=moment,
            provider_transfer_confirmed_at=moment if provider_transfer else None,
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
    run_directory = runs_root / _RUN
    run_directory.mkdir(parents=True, exist_ok=True)
    write_model(run_directory / RUN_MANIFEST_FILENAME, manifest)


def _step_b(runs_root: Path) -> Path:
    return step_dir(_RUN, StepId.B_DEDUPLICATED, root=runs_root)


def _seed(
    runs_root: Path,
    *,
    third: tuple[str, str] = (_THANKS, _RERUN),
    provider_transfer: bool = True,
) -> None:
    """Three step-b sessions: prose, a wholly pasted one, and ``third``."""
    _write_manifest(runs_root, provider_transfer=provider_transfer)
    directory = _step_b(runs_root)
    directory.mkdir(parents=True)
    first, second, last = (_SESSION_HASHES[name] for name in sorted(_SESSION_HASHES))
    write_jsonl_models(
        directory / "session-0001.jsonl",
        [_utterance(1, _PLAIN, session=first), _utterance(2, _MIXED, session=first)],
    )
    write_jsonl_models(directory / "session-0002.jsonl", [_utterance(3, _PASTED, session=second)])
    write_jsonl_models(
        directory / "session-0003.jsonl",
        [_utterance(4, third[0], session=last), _utterance(5, third[1], session=last)],
    )
    write_session_index(directory, dict(_SESSION_HASHES))


def _source(runs_root: Path, file_name: str) -> list[NormalizedUtterance]:
    return read_session(_step_b(runs_root) / file_name)


def _judge(path: Path, source: Sequence[NormalizedUtterance], texts: Sequence[str]) -> None:
    """Write the step-c file an agent would leave: same items, ``texts`` kept."""
    rows = [
        {**item.model_dump(mode="json"), "text": text}
        for item, text in zip(source, texts, strict=True)
    ]
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
    )


def _judge_all(runs_root: Path, out: Path, third: Sequence[str]) -> None:
    """Judge all three sessions, with only session 3 left for a test to choose.

    Sessions 1 and 2 are always judged the same way — the pasted lint output
    goes, the prose stays, the wholly pasted utterance comes back empty — so a
    test says only what happened in session 3.
    """
    _judge(out / "session-0001.jsonl", _source(runs_root, "session-0001.jsonl"), [_PLAIN, _KEPT])
    _judge(out / "session-0002.jsonl", _source(runs_root, "session-0002.jsonl"), [""])
    _judge(out / "session-0003.jsonl", _source(runs_root, "session-0003.jsonl"), third)


def _prepare_and_judge(runs_root: Path, third: Sequence[str]) -> Path:
    """Prepare step c, write every judged file, and return the step-c directory."""
    prepared = authorship.prepare(_RUN, runs_root=runs_root)
    out = Path(prepared.output_dir)
    _judge_all(runs_root, out, third)
    return out


def _span_code(authored: str, source: str) -> str | None:
    found = span_diagnostic(authored, source, item_ref="session-0001.jsonl")
    return None if found is None else found.code


def _session_code(
    source: list[NormalizedUtterance], authored: list[NormalizedUtterance]
) -> str | None:
    found = verify_session(source, authored, item_ref="session-0001.jsonl")
    return None if found is None else found.code


# --- the span scan ---------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "authored"),
    [
        ("the whole utterance", _MIXED),
        ("whole lines in their original order", "fix those issues\napp.ts:14:3 error"),
        ("a span from inside one line", "npm run lint"),
        ("two spans that touch but do not overlap", "fix those\n issues"),
        ("nothing at all, because none of it was theirs", ""),
    ],
)
def test_a_span_the_learner_really_wrote_is_accepted_however_the_agent_cut_it(
    name: str, authored: str
) -> None:
    # These are the shapes a correct judgment takes. A verifier that rejects any
    # of them would push honest sessions into quarantine and shrink the
    # denominator just as badly as one that accepts a paraphrase.
    assert _span_code(authored, _MIXED) is None, name


@pytest.mark.parametrize(
    ("name", "authored", "source", "code"),
    [
        (
            "invented outright",
            "we should ship this on Friday",
            _MIXED,
            "AUTHORSHIP_SPAN_NOT_VERBATIM",
        ),
        ("paraphrased", "fix these issues", _MIXED, "AUTHORSHIP_SPAN_NOT_VERBATIM"),
        ("translated", "fix the deploy script today", _RUSSIAN, "AUTHORSHIP_SPAN_NOT_VERBATIM"),
        ("tidied by one character", "fix those issues.", _MIXED, "AUTHORSHIP_SPAN_NOT_VERBATIM"),
        ("reordered", "npm run lint\nfix those issues", _MIXED, "AUTHORSHIP_SPAN_ORDER_INVALID"),
        (
            "overlapping an earlier span",
            "fix those issues\nthose issues",
            _MIXED,
            "AUTHORSHIP_SPAN_ORDER_INVALID",
        ),
        (
            "repeated to say it twice",
            "fix those issues\nfix those issues",
            _MIXED,
            "AUTHORSHIP_SPAN_ORDER_INVALID",
        ),
    ],
)
def test_a_span_the_learner_did_not_write_is_refused_with_the_reason_it_failed(
    name: str, authored: str, source: str, code: str
) -> None:
    # The two codes are not interchangeable. "Not verbatim" says the model
    # produced text; "order invalid" says it produced the learner's text but
    # rearranged or reused it, which inflates the count from real words.
    assert _span_code(authored, source) == code, name


def test_a_reordering_is_not_reported_as_an_invention() -> None:
    """Both spans are the learner's, so a plain substring check would pass.

    This is the case that distinguishes a forward scan from ``span in source``.
    A checker written the easy way accepts this, and the count it produces
    contains real words in an order the learner never used.
    """
    reordered = "npm run lint\nfix those issues"
    assert all(span in _MIXED for span in reordered.split("\n"))
    assert _span_code(reordered, _MIXED) == "AUTHORSHIP_SPAN_ORDER_INVALID"


# --- one file against its step-b session -----------------------------------


def test_a_faithful_repeat_of_the_step_b_session_has_nothing_to_report() -> None:
    source = [_utterance(1, _PLAIN), _utterance(2, _MIXED)]
    assert _session_code(source, [_utterance(1, _PLAIN), _utterance(2, _KEPT)]) is None


@pytest.mark.parametrize(
    ("name", "authored"),
    [
        ("an item dropped", [_utterance(1, _PLAIN)]),
        ("nothing returned at all", []),
        ("an item invented", [_utterance(1, _PLAIN), _utterance(2, _MIXED), _utterance(2, _MIXED)]),
    ],
)
def test_an_item_count_that_changed_quarantines_the_file(
    name: str, authored: list[NormalizedUtterance]
) -> None:
    # Step c answers item for item. Letting the count move would let a session
    # lose utterances silently, which is exactly the loss no later step could
    # detect: the file is still there and still parses.
    source = [_utterance(1, _PLAIN), _utterance(2, _MIXED)]
    assert _session_code(source, authored) == "CARDINALITY_MISMATCH", name


def test_items_returned_in_a_different_order_quarantine_the_file() -> None:
    source = [_utterance(1, "alpha beta"), _utterance(2, "gamma delta")]
    swapped = [_utterance(2, "gamma delta"), _utterance(1, "alpha beta")]
    assert _session_code(source, swapped) == "CARDINALITY_MISMATCH"


def test_an_item_naming_an_utterance_this_session_never_held_is_named_as_such() -> None:
    source = [_utterance(1, _PLAIN)]
    assert _session_code(source, [_utterance(9, _PLAIN)]) == "AUTHORSHIP_UNKNOWN_UTTERANCE"


def test_two_items_covering_the_same_utterance_are_named_as_such() -> None:
    source = [_utterance(1, "alpha"), _utterance(2, "beta")]
    doubled = [_utterance(1, "alpha"), _utterance(1, "alpha")]
    assert _session_code(source, doubled) == "AUTHORSHIP_DUPLICATE_DECISION"


@pytest.mark.parametrize(
    ("name", "update"),
    [
        ("the modality", {"modality": Modality.SPOKEN_ASR}),
        ("the confidence the adapter established", {"authorship_confidence": 0.1}),
        ("when it was said", {"timestamp": datetime(2001, 1, 1, tzinfo=UTC)}),
        ("which adapter found it", {"source_adapter": "cursor"}),
        ("whether the text is verbatim", {"text_status": TextStatus.CLEANED}),
    ],
)
def test_an_item_that_changed_a_field_other_than_text_quarantines_the_file(
    name: str, update: dict[str, object]
) -> None:
    # Everything but `text` is provenance the adapters established. A rewritten
    # timestamp or modality would travel unchecked into every later step, and
    # the modality decides which grammar rules a mistake is judged against.
    source = [_utterance(1, _PLAIN)]
    drifted = _utterance(1, _PLAIN).model_copy(update=update)
    assert _session_code(source, [drifted]) == "SCHEMA_INVALID_VALUE", name


def test_a_timestamp_the_agent_spelled_differently_still_compares_equal() -> None:
    """``Z`` and ``+00:00`` are the same instant, and models write both.

    Comparing the raw lines instead of the parsed models would quarantine an
    otherwise perfect judgment over a formatting choice nobody made
    deliberately.
    """
    row = json.loads(_utterance(1, _MIXED).model_dump_json())
    row["timestamp"] = "2026-08-01T12:01:00Z"
    row["text"] = _KEPT
    parsed = read_authored(json.dumps(row).encode("utf-8"), item_ref="session-0001.jsonl")
    assert not isinstance(parsed, Diagnostic), parsed
    assert _session_code([_utterance(1, _MIXED)], parsed) is None


# --- reading what the agent wrote ------------------------------------------


@pytest.mark.parametrize(
    ("name", "raw", "code"),
    [
        ("truncated mid-object", b"{oops\n", "SCHEMA_INVALID_JSON"),
        ("not UTF-8 at all", b"\xff\xfe\n", "SCHEMA_INVALID_JSON"),
        ("a bare string per line", b'"a string"\n', "SCHEMA_INVALID_VALUE"),
        ("only the fields it felt like", b'{"utterance_id": "u-001"}\n', "SCHEMA_MISSING_FIELD"),
    ],
)
def test_a_file_the_agent_mangled_is_diagnosed_rather_than_raised(
    name: str, raw: bytes, code: str
) -> None:
    # This file is model output. A malformed one has to quarantine its session
    # and let the other sessions finish, not end the run with a traceback.
    found = read_authored(raw, item_ref="session-0001.jsonl")
    assert isinstance(found, Diagnostic), name
    assert found.code == code, name


def test_a_field_the_agent_added_of_its_own_accord_is_refused() -> None:
    # An extra field is a model deciding the schema is a suggestion. Accepting
    # it would let commentary or a confidence score ride into a later step.
    row = json.loads(_utterance(1, _PLAIN).model_dump_json())
    row["note"] = "I was not sure about this one"
    found = read_authored(json.dumps(row).encode("utf-8"), item_ref="session-0001.jsonl")
    assert isinstance(found, Diagnostic)
    assert found.code == "SCHEMA_UNEXPECTED_FIELD"


def test_a_blank_line_between_records_is_not_a_defect() -> None:
    raw = (_utterance(1, _PLAIN).model_dump_json() + "\n\n").encode("utf-8")
    parsed = read_authored(raw, item_ref="session-0001.jsonl")
    assert not isinstance(parsed, Diagnostic)
    assert len(parsed) == 1


# --- preparing the step ----------------------------------------------------


def test_prepare_refuses_a_run_without_provider_transfer_consent(tmp_path: Path) -> None:
    # Preparing is the moment the learner's sentences become provider-bound:
    # the step-c directory exists to be read by an agent. Specification 2.2
    # makes that consent per-run, so nothing may be written before it is found.
    _seed(tmp_path, provider_transfer=False)
    with pytest.raises(MissingConsentError):
        authorship.prepare(_RUN, runs_root=tmp_path)
    assert not authorship.authored_dir(_RUN, runs_root=tmp_path).exists()


def test_prepare_refuses_a_run_with_no_manifest_at_all(tmp_path: Path) -> None:
    # A run with no recorded state has recorded no agreement either.
    directory = _step_b(tmp_path)
    directory.mkdir(parents=True)
    write_jsonl_models(directory / "session-0001.jsonl", [_utterance(1, _PLAIN)])
    with pytest.raises(RunStoreError):
        authorship.prepare(_RUN, runs_root=tmp_path)
    assert not authorship.authored_dir(_RUN, runs_root=tmp_path).exists()


def test_prepare_names_every_step_b_session_and_where_its_answer_goes(tmp_path: Path) -> None:
    _seed(tmp_path)
    prepared = authorship.prepare(_RUN, runs_root=tmp_path)
    assert [entry.file_name for entry in prepared.sessions] == sorted(_SESSION_HASHES)
    assert prepared.utterance_count == 5
    assert not prepared.repair_only
    out = Path(prepared.output_dir)
    for entry in prepared.sessions:
        assert Path(entry.input_path).parent == _step_b(tmp_path)
        assert Path(entry.output_path) == out / entry.file_name


def test_prepare_carries_the_session_index_across_so_the_names_still_mean_something(
    tmp_path: Path,
) -> None:
    # File names are opaque sequence numbers. Without the index travelling with
    # them, step c's output cannot be traced back to a session at all.
    _seed(tmp_path)
    prepared = authorship.prepare(_RUN, runs_root=tmp_path)
    assert read_index(Path(prepared.output_dir)) == _SESSION_HASHES


def test_prepare_counts_every_word_the_agent_must_read_not_only_the_english_ones(
    tmp_path: Path,
) -> None:
    # This number is what the judgment costs, and the agent reads a Russian
    # sentence as attentively as an English one. The English denominator is a
    # different number, counted after the judgment.
    _seed(tmp_path, third=(_THANKS, _RUSSIAN))
    prepared = authorship.prepare(_RUN, runs_root=tmp_path)
    third = next(e for e in prepared.sessions if e.file_name == "session-0003.jsonl")
    assert third.word_count == count_words(_THANKS) + count_words(_RUSSIAN)
    assert english_words(_RUSSIAN) == 0


def test_prepare_marks_the_sessions_an_interrupted_run_already_judged(tmp_path: Path) -> None:
    # Resuming should cost only the sessions still missing. Reporting every
    # file as unwritten would pay for every judgment a second time.
    _seed(tmp_path)
    out = Path(authorship.prepare(_RUN, runs_root=tmp_path).output_dir)
    _judge(out / "session-0001.jsonl", _source(tmp_path, "session-0001.jsonl"), [_PLAIN, _KEPT])
    again = authorship.prepare(_RUN, runs_root=tmp_path)
    written = {entry.file_name: entry.already_written for entry in again.sessions}
    assert written == {
        "session-0001.jsonl": True,
        "session-0002.jsonl": False,
        "session-0003.jsonl": False,
    }


def test_prepare_refuses_a_run_where_step_b_produced_nothing(tmp_path: Path) -> None:
    # Preparing zero sessions would send the agents to read nothing and let the
    # step report a clean zero for work nobody did.
    _write_manifest(tmp_path, provider_transfer=True)
    _step_b(tmp_path).mkdir(parents=True)
    with pytest.raises(ValueError, match="no session files"):
        authorship.prepare(_RUN, runs_root=tmp_path)


# --- applying the judgments ------------------------------------------------


def test_a_faithful_judgment_promotes_every_session_and_counts_only_kept_text(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    out = _prepare_and_judge(tmp_path, [_THANKS, _RERUN])
    result = authorship.apply_authorship(_RUN, runs_root=tmp_path)

    assert result.diagnostics == []
    assert (result.sessions_in, result.sessions_verified, result.sessions_quarantined) == (3, 3, 0)
    assert result.utterances_in == result.index.utterance_count == 5
    assert result.words_after == _KEPT_WORDS + count_words(_THANKS) + count_words(_RERUN)
    # The pasted lint output the agent dropped never reaches the denominator.
    assert result.words_after < result.words_before
    assert not (out / QUARANTINE_DIR_NAME).exists()
    assert authorship.read_repair_list(_RUN, runs_root=tmp_path) == []
    assert verify_corpus(_RUN, runs_root=tmp_path) == []

    manifest = load_manifest(_RUN, root=tmp_path)
    assert manifest.steps[StepId.C_AUTHORED].status is StepStatus.PROMOTED
    assert manifest.steps[StepId.C_AUTHORED].current_artifact_hash == result.index.corpus_sha256


def test_keeping_every_word_leaves_the_denominator_exactly_where_it_was(tmp_path: Path) -> None:
    """Before and after are both English words, so the delta is the judgment.

    Counting all words before and English words after would fold two unrelated
    reductions into one number and then report it as what the agent removed —
    a run of mostly Russian would look like the model had deleted most of it.
    """
    _seed(tmp_path, third=(_THANKS, _RUSSIAN))
    out = _prepare_and_judge(tmp_path, [_THANKS, _RUSSIAN])
    _judge(out / "session-0001.jsonl", _source(tmp_path, "session-0001.jsonl"), [_PLAIN, _MIXED])
    _judge(out / "session-0002.jsonl", _source(tmp_path, "session-0002.jsonl"), [_PASTED])
    result = authorship.apply_authorship(_RUN, runs_root=tmp_path)
    assert result.diagnostics == []
    assert result.words_after == result.words_before


def test_an_utterance_step_c_emptied_keeps_its_line(tmp_path: Path) -> None:
    # A session whose text was entirely someone else's is a legal outcome, and
    # it is not the same thing as a session that lost an utterance. Deleting the
    # line makes the two indistinguishable, so the file must keep its place.
    _seed(tmp_path)
    out = _prepare_and_judge(tmp_path, [_THANKS, _RERUN])
    result = authorship.apply_authorship(_RUN, runs_root=tmp_path)

    emptied = next(e for e in result.index.sessions if e.file_name == "session-0002.jsonl")
    assert (emptied.utterance_count, emptied.word_count) == (1, 0)
    kept = read_session(out / "session-0002.jsonl")
    assert [item.utterance_id for item in kept] == ["u-003"]
    assert kept[0].text == ""

    # The same session with the line deleted instead is a defect, not a variant.
    (out / "session-0002.jsonl").write_text("", encoding="utf-8")
    second = authorship.apply_authorship(_RUN, runs_root=tmp_path)
    assert [d.code for d in second.diagnostics] == ["CARDINALITY_MISMATCH"]
    assert second.sessions_quarantined == 1


@pytest.mark.parametrize(
    ("name", "source", "authored", "code"),
    [
        (
            "a sentence the model invented",
            (_THANKS, _RERUN),
            ("we should ship this on Friday", _RERUN),
            "AUTHORSHIP_SPAN_NOT_VERBATIM",
        ),
        (
            "the learner's point in the model's words",
            (_THANKS, _RERUN),
            ("I agree that the second variant is better.", _RERUN),
            "AUTHORSHIP_SPAN_NOT_VERBATIM",
        ),
        (
            "a translation of what they said",
            (_THANKS, _RUSSIAN),
            (_THANKS, "we need to fix the deploy script today"),
            "AUTHORSHIP_SPAN_NOT_VERBATIM",
        ),
        (
            "their words in an order they never used",
            (_THANKS, _RERUN),
            ("Thanks.\nI am agree", _RERUN),
            "AUTHORSHIP_SPAN_ORDER_INVALID",
        ),
        (
            "one span counted twice through an overlap",
            (_THANKS, _RERUN),
            ("I am agree that the second\nsecond variant", _RERUN),
            "AUTHORSHIP_SPAN_ORDER_INVALID",
        ),
    ],
)
def test_a_bad_span_quarantines_its_whole_file_and_the_words_it_stood_beside(
    tmp_path: Path,
    name: str,
    source: tuple[str, str],
    authored: tuple[str, str],
    code: str,
) -> None:
    # The file is the unit of work, so there is no partial acceptance: the
    # second utterance of session 3 was judged correctly and still loses its
    # words. Accepting the good half would leave a corpus whose provenance is
    # half checked and wholly counted.
    _seed(tmp_path, third=source)
    out = _prepare_and_judge(tmp_path, authored)
    result = authorship.apply_authorship(_RUN, runs_root=tmp_path)

    assert [d.code for d in result.diagnostics] == [code], name
    assert result.diagnostics[0].item_ref == "session-0003.jsonl"
    assert result.sessions_quarantined == 1
    assert [e.file_name for e in result.index.sessions] == [
        "session-0001.jsonl",
        "session-0002.jsonl",
    ]
    assert result.words_after == _KEPT_WORDS
    assert result.index.quarantined_utterance_count == 2
    assert not (out / "session-0003.jsonl").exists()
    assert (out / QUARANTINE_DIR_NAME / "session-0003.jsonl").is_file()
    assert authorship.read_repair_list(_RUN, runs_root=tmp_path) == ["session-0003.jsonl"]
    # What survived is a corpus a fresh reader can still verify on its own.
    assert verify_corpus(_RUN, runs_root=tmp_path) == []


def test_a_session_nobody_judged_is_quarantined_and_asked_again(tmp_path: Path) -> None:
    # An agent that crashed leaves no file. Treating that as "nothing to keep"
    # would silently drop the session's words from the denominator.
    _seed(tmp_path)
    prepared = authorship.prepare(_RUN, runs_root=tmp_path)
    out = Path(prepared.output_dir)
    _judge(out / "session-0001.jsonl", _source(tmp_path, "session-0001.jsonl"), [_PLAIN, _KEPT])
    _judge(out / "session-0002.jsonl", _source(tmp_path, "session-0002.jsonl"), [""])

    result = authorship.apply_authorship(_RUN, runs_root=tmp_path)
    assert [d.code for d in result.diagnostics] == ["LINEAGE_MISSING_INPUT"]
    assert result.sessions_quarantined == 1
    assert result.index.quarantined_utterance_count == 2
    assert authorship.read_repair_list(_RUN, runs_root=tmp_path) == ["session-0003.jsonl"]


def test_a_step_c_file_step_b_never_produced_is_quarantined_but_never_repaired(
    tmp_path: Path,
) -> None:
    """A file nothing asked for holds sentences nothing counted.

    It cannot be repaired, because no step-b session is waiting for it — asking
    for it again would ask about a session that does not exist. It still has to
    leave the corpus directory, or a later step reads utterances the index
    never counted.
    """
    _seed(tmp_path)
    out = _prepare_and_judge(tmp_path, [_THANKS, _RERUN])
    _judge(out / "session-0042.jsonl", _source(tmp_path, "session-0002.jsonl"), [""])

    result = authorship.apply_authorship(_RUN, runs_root=tmp_path)
    assert [(d.code, d.item_ref) for d in result.diagnostics] == [
        ("CARDINALITY_MISMATCH", "session-0042.jsonl")
    ]
    assert result.sessions_verified == 3
    assert not (out / "session-0042.jsonl").exists()
    assert (out / QUARANTINE_DIR_NAME / "session-0042.jsonl").is_file()
    assert authorship.read_repair_list(_RUN, runs_root=tmp_path) == []
    assert verify_corpus(_RUN, runs_root=tmp_path) == []


def test_a_quarantined_session_is_asked_again_and_rejoins_the_corpus(tmp_path: Path) -> None:
    """Specification 6.4 allows a bounded repair, and for a while there was none.

    The failed session was named in a file that nothing read, so its words left
    the denominator with no way back short of redoing the whole run.
    """
    _seed(tmp_path)
    out = _prepare_and_judge(tmp_path, ["I agree the second variant is better.", _RERUN])
    first = authorship.apply_authorship(_RUN, runs_root=tmp_path)
    assert first.sessions_quarantined == 1

    repair = authorship.prepare(_RUN, runs_root=tmp_path, repair_only=True)
    assert repair.repair_only
    # Only the failed session, and it is waiting to be written, not already done.
    assert [entry.file_name for entry in repair.sessions] == ["session-0003.jsonl"]
    assert repair.sessions[0].already_written is False

    _judge(out / "session-0003.jsonl", _source(tmp_path, "session-0003.jsonl"), [_THANKS, _RERUN])
    second = authorship.apply_authorship(_RUN, runs_root=tmp_path)
    assert second.diagnostics == []
    assert second.sessions_verified == 3
    assert second.words_after == _KEPT_WORDS + count_words(_THANKS) + count_words(_RERUN)
    # The stale quarantined copy would otherwise say the session still failed.
    assert not (out / QUARANTINE_DIR_NAME / "session-0003.jsonl").exists()
    assert authorship.read_repair_list(_RUN, runs_root=tmp_path) == []
    assert verify_corpus(_RUN, runs_root=tmp_path) == []


def test_a_repair_pass_with_nothing_to_repair_is_refused(tmp_path: Path) -> None:
    # An empty repair pass would send the agents to read nothing and report a
    # clean zero, which is the shape of failure this project keeps finding.
    _seed(tmp_path)
    _prepare_and_judge(tmp_path, [_THANKS, _RERUN])
    authorship.apply_authorship(_RUN, runs_root=tmp_path)
    with pytest.raises(ValueError, match="listed for repair"):
        authorship.prepare(_RUN, runs_root=tmp_path, repair_only=True)


def test_a_promoted_session_file_edited_afterwards_no_longer_matches_its_recorded_hash(
    tmp_path: Path,
) -> None:
    """The per-file hash is what makes the verification survive the step.

    Step c verifies text it then leaves on disk for four more steps to read. The
    hash recorded here is the only thing that says the file a later reader opens
    is still the file the span scan accepted.
    """
    _seed(tmp_path)
    out = _prepare_and_judge(tmp_path, [_THANKS, _RERUN])
    authorship.apply_authorship(_RUN, runs_root=tmp_path)
    assert verify_corpus(_RUN, runs_root=tmp_path) == []

    target = out / "session-0001.jsonl"
    target.write_text(target.read_text(encoding="utf-8").replace("agree", "agreed"), "utf-8")
    assert [d.code for d in verify_corpus(_RUN, runs_root=tmp_path)] == ["LINEAGE_HASH_MISMATCH"]


def test_the_corpus_digest_summarizes_the_file_set_and_not_its_listing_order() -> None:
    # There is no pooled corpus file, so this digest is the one number the run
    # manifest can point at. It must depend on which files were verified and
    # what they contain, and on nothing else.
    first = AuthoredSession(
        file_name="session-0001.jsonl", utterance_count=2, word_count=9, sha256="1" * 64
    )
    second = AuthoredSession(
        file_name="session-0002.jsonl", utterance_count=1, word_count=0, sha256="2" * 64
    )
    assert corpus_digest([first, second]) == corpus_digest([second, first])
    changed = second.model_copy(update={"sha256": "3" * 64})
    assert corpus_digest([first, changed]) != corpus_digest([first, second])


# --- the command line ------------------------------------------------------


def test_the_prepare_command_reports_the_session_files_to_judge(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(tmp_path)
    argv = ["--run-id", _RUN, "--runs-root", str(tmp_path), "--prepare"]
    assert authorship.main(argv) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [entry["file_name"] for entry in payload["sessions"]] == sorted(_SESSION_HASHES)


def test_the_apply_command_promotes_the_step_and_still_fails_when_a_session_was_lost(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Promotion and a non-zero exit are both correct, and both are needed.

    The verified files are durable, so the run can continue after a bounded
    repair rather than restart. Nothing may read the exit code as "every
    session was judged", which is why the command fails anyway.
    """
    _seed(tmp_path)
    _prepare_and_judge(tmp_path, ["a sentence nobody typed", _RERUN])
    argv = ["--run-id", _RUN, "--runs-root", str(tmp_path), "--apply"]
    assert authorship.main(argv) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["sessions_quarantined"] == 1
    assert payload["diagnostic_codes"] == {"AUTHORSHIP_SPAN_NOT_VERBATIM": 1}
    assert payload["tokenizer_version"] == TOKENIZER_VERSION

    manifest = load_manifest(_RUN, root=tmp_path)
    assert manifest.steps[StepId.C_AUTHORED].status is StepStatus.PROMOTED


def test_the_apply_command_succeeds_when_every_session_was_judged(tmp_path: Path) -> None:
    _seed(tmp_path)
    _prepare_and_judge(tmp_path, [_THANKS, _RERUN])
    argv = ["--run-id", _RUN, "--runs-root", str(tmp_path), "--apply"]
    assert authorship.main(argv) == 0


def test_repair_only_is_refused_with_apply_rather_than_quietly_ignored(tmp_path: Path) -> None:
    # It selects what to prepare. Accepting it here would let an operator
    # believe they had re-applied only the failed sessions.
    _seed(tmp_path)
    argv = ["--run-id", _RUN, "--runs-root", str(tmp_path), "--apply", "--repair-only"]
    with pytest.raises(SystemExit) as raised:
        authorship.main(argv)
    assert raised.value.code == 2

"""The short form must expand to the artifact that exists today, exactly.

This is the whole safety argument for the change. Agents stop writing artifacts
and start writing decisions, but the artifacts are unchanged — so ``verify_corpus``,
``build_review``, ``step_layout`` and every end-to-end test keep working as a
regression proof rather than being rewritten alongside the thing they check.

That only holds while expansion is lossless. Derive the short form from an
artifact, expand it back, and the result must be the artifact. Verified by hand
on a real run first: 449 of 449 step-c utterances round-tripped byte-identical.
"""

from datetime import UTC, datetime

import pytest

from glite_english_audit.artifacts.enums import ExampleType, Modality, TextStatus
from glite_english_audit.artifacts.models import (
    EvidenceSpan,
    MistakeRecord,
    NormalizedUtterance,
)
from glite_english_audit.pipeline.agent_io import (
    AuthoredLine,
    DropList,
    MistakeDraft,
    expand_authored,
    expand_mistakes,
    expand_verified,
    project_records,
    project_utterances,
)

_NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


def _utterance(
    index: int, text: str, *, modality: Modality = Modality.WRITTEN
) -> NormalizedUtterance:
    return NormalizedUtterance(
        utterance_id=f"claude_code-0123456789abcdef-{index:04d}",
        source_adapter="claude_code",
        adapter_version="1.0.0",
        session_hash="a" * 64,
        timestamp=_NOW,
        text=text,
        modality=modality,
        text_status=TextStatus.VERBATIM,
        authorship_confidence=0.95,
        authorship_basis="explicit_user_role",
        source_path_hash="c" * 64,
        destination_app="code_editor",
        content_flags=["possible_paste"],
    )


def _record(utterance: NormalizedUtterance, start: int, end: int) -> MistakeRecord:
    return MistakeRecord(
        utterance_id=utterance.utterance_id,
        evidence_span=EvidenceSpan(start=start, end=end),
        mistake="Used the past form where the infinitive belongs.",
        rule="After 'to', English uses the base form of the verb.",
        example="I wanted to built a shelf last weekend.",
        example_type=ExampleType.SYNTHETIC,
        source_type=utterance.source_adapter,
        modality=utterance.modality,
    )


_TEXTS = [
    "now check this repo and tell me what it does",
    "",  # step c empties an utterance nobody authored; it keeps its line
    "first line of a pasted note\nsecond line the learner wrote",
    "x",
    "  leading and trailing spaces are part of the text  ",
    "неанглийский текст between English ones",
]


def test_step_c_round_trips_every_shape() -> None:
    source = [_utterance(n, text) for n, text in enumerate(_TEXTS, 1)]
    # What the agent decided, derived from the artifact that exists today.
    decisions = [AuthoredLine(i=n, text=u.text) for n, u in enumerate(source, 1)]
    expanded, diagnostics = expand_authored(source, decisions, item_ref="session-0001.jsonl")
    assert diagnostics == []
    assert expanded == source


def test_step_c_round_trips_a_partial_judgment() -> None:
    # The case the artifact is built for: some words kept, the rest someone
    # else's. The expander must not care which.
    source = [_utterance(1, "please review this\nirrelevant pasted log line\nand tell me")]
    kept = "please review this\nand tell me"
    expanded, diagnostics = expand_authored(
        source, [AuthoredLine(i=1, text=kept)], item_ref="session-0001.jsonl"
    )
    assert diagnostics == []
    assert expanded[0].text == kept
    assert expanded[0].model_dump(exclude={"text"}) == source[0].model_dump(exclude={"text"})


def test_step_c_round_trips_an_empty_session() -> None:
    expanded, diagnostics = expand_authored([], [], item_ref="session-0001.jsonl")
    assert (expanded, diagnostics) == ([], [])


def test_step_d_round_trips_a_record() -> None:
    source = [_utterance(1, "I wanted to built a shelf")]
    original = _record(source[0], 14, 22)
    draft = MistakeDraft(
        i=1,
        span=(original.evidence_span.start, original.evidence_span.end),
        mistake=original.mistake,
        rule=original.rule,
        example=original.example,
        example_type=original.example_type,
    )
    expanded, diagnostics = expand_mistakes(source, [draft], item_ref="session-0001.jsonl")
    assert diagnostics == []
    assert expanded == [original]
    assert expanded[0].record_id == original.record_id


def test_step_d_rederives_the_three_fields_the_agent_no_longer_sends() -> None:
    """utterance_id, source_type and modality are copies of the utterance.

    The skill used to instruct the agent to copy the first two and resolve the
    third, which made each of them something a model could get wrong.
    """
    source = [_utterance(1, "I have wrote it", modality=Modality.SPOKEN_ASR)]
    draft = MistakeDraft(
        i=1,
        span=(7, 12),
        mistake="Used the past participle where the past simple belongs.",
        rule="English forms the past simple of 'write' as 'wrote'.",
        example="I have wrote the report already.",
        example_type=ExampleType.SYNTHETIC,
    )
    expanded, diagnostics = expand_mistakes(source, [draft], item_ref="session-0001.jsonl")
    assert diagnostics == []
    assert expanded[0].utterance_id == source[0].utterance_id
    assert expanded[0].source_type == "claude_code"
    assert expanded[0].modality is Modality.SPOKEN_ASR


@pytest.mark.parametrize(
    ("stored", "expected"),
    [(Modality.WRITTEN, Modality.WRITTEN), (Modality.UNKNOWN, Modality.WRITTEN)],
)
def test_step_d_resolves_unknown_modality_to_written(stored: Modality, expected: Modality) -> None:
    # MistakeRecord forbids unknown, so an utterance whose modality nobody
    # established becomes written. That resolution is an input-provenance
    # convention, not a claim about physical typing.
    source = [_utterance(1, "some text to cite", modality=stored)]
    expanded, _ = expand_mistakes(
        source,
        [
            MistakeDraft(
                i=1,
                span=(0, 4),
                mistake="m",
                rule="r",
                example="an invented sentence",
                example_type=ExampleType.SYNTHETIC,
            )
        ],
        item_ref="session-0001.jsonl",
    )
    assert expanded[0].modality is expected


def test_step_e_round_trips_a_pass_through() -> None:
    source = [_utterance(1, "I wanted to built a shelf")]
    produced = [_record(source[0], 0, 6), _record(source[0], 14, 22)]
    expanded, diagnostics = expand_verified(
        produced, DropList(drop=[]), item_ref="session-0001.jsonl"
    )
    assert diagnostics == []
    assert expanded == produced


def test_step_e_removes_exactly_what_it_named() -> None:
    source = [_utterance(1, "I wanted to built a shelf")]
    produced = [_record(source[0], 0, 6), _record(source[0], 14, 22)]
    expanded, diagnostics = expand_verified(
        produced, DropList(drop=[1]), item_ref="session-0001.jsonl"
    )
    assert diagnostics == []
    assert expanded == [produced[1]]


def test_step_e_cannot_alter_a_record_it_keeps() -> None:
    """The point of the drop list, and why verify.py loses four checks.

    Every kept record is the object step d wrote. There is no path by which a
    step-e file carries a record that differs from its step-d original, so
    'step e altered a record' stops being a thing to detect.
    """
    source = [_utterance(1, "I wanted to built a shelf")]
    produced = [_record(source[0], 14, 22)]
    expanded, _ = expand_verified(produced, DropList(drop=[]), item_ref="session-0001.jsonl")
    assert expanded[0] is produced[0]


def test_the_projection_carries_nothing_that_identifies_anyone() -> None:
    """Privacy by construction, the way ProgressState is pinned.

    The field set is the claim: an agent cannot be sent a session hash, a path
    hash or an utterance ID because the model it is sent has nowhere to put one.
    """
    source = [_utterance(1, "some text")]
    projected = project_utterances(source)
    assert set(type(projected[0]).model_fields) == {"i", "modality", "text", "content_flags"}
    serialized = projected[0].model_dump_json()
    assert source[0].session_hash not in serialized
    assert source[0].source_path_hash not in serialized
    assert source[0].utterance_id not in serialized


def test_the_step_e_projection_hides_the_addresses_it_must_not_judge() -> None:
    source = [_utterance(1, "I wanted to built a shelf")]
    projected = project_records([_record(source[0], 14, 22)])
    assert set(type(projected[0]).model_fields) == {
        "i",
        "mistake",
        "rule",
        "example",
        "example_type",
    }
    serialized = projected[0].model_dump_json()
    assert source[0].utterance_id not in serialized


def test_the_projection_numbers_from_one_in_file_order() -> None:
    source = [_utterance(n, f"line {n}") for n in range(1, 4)]
    assert [item.i for item in project_utterances(source)] == [1, 2, 3]
    # The adapter's own paste heuristics are evidence about authorship that the
    # text alone does not carry, so they travel; they name no one.
    assert project_utterances(source)[0].content_flags == ["possible_paste"]
    assert [item.text for item in project_utterances(source)] == ["line 1", "line 2", "line 3"]


def test_step_c_refuses_an_answer_that_skips_a_line() -> None:
    # Step c's file answers step b line for line. A missing index is a question
    # nobody answered, and guessing which one would put the wrong words in a
    # session that then passes every later check.
    source = [_utterance(1, "first"), _utterance(2, "second")]
    _, diagnostics = expand_authored(
        source, [AuthoredLine(i=1, text="first")], item_ref="session-0001.jsonl"
    )
    assert [d.code for d in diagnostics] == ["CARDINALITY_MISMATCH"]
    assert "1 of the 2" in diagnostics[0].message


def test_step_c_refuses_two_answers_for_one_line() -> None:
    source = [_utterance(1, "first"), _utterance(2, "second")]
    _, diagnostics = expand_authored(
        source,
        [AuthoredLine(i=1, text="first"), AuthoredLine(i=1, text="first")],
        item_ref="session-0001.jsonl",
    )
    # The code carries over from when the agent named utterances directly: this
    # is still more than one decision covering one utterance.
    assert [d.code for d in diagnostics] == ["AUTHORSHIP_DUPLICATE_DECISION"]


def test_an_index_past_the_end_of_the_session_is_refused() -> None:
    # The failure the index format introduces, and the one the old format could
    # not have: an utterance_id either matched or it did not.
    source = [_utterance(1, "only one")]
    _, diagnostics = expand_authored(
        source,
        [AuthoredLine(i=1, text="only one"), AuthoredLine(i=2, text="invented")],
        item_ref="session-0001.jsonl",
    )
    assert [d.code for d in diagnostics] == ["AUTHORSHIP_UNKNOWN_UTTERANCE"]
    assert "item 2 of a session holding 1" in diagnostics[0].message


def test_step_d_may_answer_some_utterances_and_not_others() -> None:
    # Unlike step c, most utterances hold no mistake. Requiring full coverage
    # here would make an honest empty answer a failure.
    source = [_utterance(1, "clean sentence"), _utterance(2, "I have wrote it")]
    expanded, diagnostics = expand_mistakes(
        source,
        [
            MistakeDraft(
                i=2,
                span=(7, 12),
                mistake="m",
                rule="r",
                example="an invented sentence here",
                example_type=ExampleType.SYNTHETIC,
            )
        ],
        item_ref="session-0001.jsonl",
    )
    assert diagnostics == []
    assert len(expanded) == 1
    assert expanded[0].utterance_id == source[1].utterance_id


def test_step_d_may_cite_one_utterance_twice() -> None:
    # Two independent errors in one sentence are two records. Whether their
    # spans collide is the overlap check's question, not the expander's.
    source = [_utterance(1, "I have wrote it and sended it")]
    drafts = [
        MistakeDraft(
            i=1,
            span=(7, 12),
            mistake="m1",
            rule="r1",
            example="e one",
            example_type=ExampleType.SYNTHETIC,
        ),
        MistakeDraft(
            i=1,
            span=(20, 26),
            mistake="m2",
            rule="r2",
            example="e two",
            example_type=ExampleType.SYNTHETIC,
        ),
    ]
    expanded, diagnostics = expand_mistakes(source, drafts, item_ref="session-0001.jsonl")
    assert diagnostics == []
    assert len({record.record_id for record in expanded}) == 2


def test_step_d_refuses_a_span_that_is_not_a_range() -> None:
    source = [_utterance(1, "some text")]
    _, diagnostics = expand_mistakes(
        source,
        [
            MistakeDraft(
                i=1,
                span=(5, 5),
                mistake="m",
                rule="r",
                example="an example",
                example_type=ExampleType.SYNTHETIC,
            )
        ],
        item_ref="session-0001.jsonl",
    )
    assert [d.code for d in diagnostics] == ["SCHEMA_INVALID_VALUE"]


def test_step_e_refuses_to_drop_a_record_that_is_not_there() -> None:
    source = [_utterance(1, "I wanted to built a shelf")]
    produced = [_record(source[0], 14, 22)]
    _, diagnostics = expand_verified(produced, DropList(drop=[2]), item_ref="session-0001.jsonl")
    assert [d.code for d in diagnostics] == ["CARDINALITY_MISMATCH"]


def test_step_e_refuses_to_name_one_record_twice() -> None:
    # Withheld records are counted and reported to the user, so naming one
    # twice would overstate what was held back.
    source = [_utterance(1, "I wanted to built a shelf")]
    produced = [_record(source[0], 0, 6), _record(source[0], 14, 22)]
    _, diagnostics = expand_verified(produced, DropList(drop=[1, 1]), item_ref="session-0001.jsonl")
    assert [d.code for d in diagnostics] == ["CARDINALITY_MISMATCH"]
    assert "more than once" in diagnostics[0].message


def test_step_e_may_drop_everything() -> None:
    source = [_utterance(1, "I wanted to built a shelf")]
    produced = [_record(source[0], 14, 22)]
    expanded, diagnostics = expand_verified(
        produced, DropList(drop=[1]), item_ref="session-0001.jsonl"
    )
    assert (expanded, diagnostics) == ([], [])


def test_step_d_reports_an_out_of_range_index_as_a_missing_input() -> None:
    # Step d's own verifier already says LINEAGE_MISSING_INPUT when a record
    # cites an utterance the session does not contain; an index past the end is
    # the same claim, so it gets the same code.
    source = [_utterance(1, "only one utterance")]
    _, diagnostics = expand_mistakes(
        source,
        [
            MistakeDraft(
                i=4,
                span=(0, 4),
                mistake="m",
                rule="r",
                example="an invented sentence",
                example_type=ExampleType.SYNTHETIC,
            )
        ],
        item_ref="session-0001.jsonl",
    )
    assert [d.code for d in diagnostics] == ["LINEAGE_MISSING_INPUT"]

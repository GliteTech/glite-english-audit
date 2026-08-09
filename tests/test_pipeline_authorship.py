"""Stage 3 with the model in the loop: batches out, verified decisions in.

The model chooses the spans and the tokenizer counts them. These tests hold
that split: every way a decision can fail the substring, order, or identity
checks must quarantine it, and the corpus that comes out must count exactly
the retained text and nothing else.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from glite_english_audit.artifacts.enums import (
    AgentRuntime,
    Modality,
    OsEnvironment,
    RunStatus,
    StageId,
    TextStatus,
)
from glite_english_audit.artifacts.io import read_jsonl_models, write_jsonl_models, write_model
from glite_english_audit.artifacts.manifest import (
    MANIFEST_SCHEMA_VERSION,
    CompatibilityFingerprint,
    ConsentState,
    RunManifest,
    empty_stage_map,
)
from glite_english_audit.artifacts.models import NormalizedUtterance
from glite_english_audit.consent import CONSENT_POLICY_VERSION, MissingConsentError
from glite_english_audit.normalization.tokenizer import TOKENIZER_VERSION, count_words
from glite_english_audit.paths import stage_dir
from glite_english_audit.pipeline.apply_authorship import apply_authorship
from glite_english_audit.pipeline.authorship_batches import (
    DECISIONS_DIR_NAME,
    INDEX_NAME,
    batch_dir,
    decisions_dir,
    prepare_authorship_batches,
)
from glite_english_audit.state.run_store import RUN_MANIFEST_FILENAME, RunStoreError
from glite_english_audit.verification.verify_corpus import verify_corpus

_RUN = "run-" + "2" * 32
_NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)

_PLAIN = "I am agree that the second variant reads better."
_MIXED = "fix those issues\n$ npm run lint\napp.ts:14:3 error 'cfg' is assigned but never used"
_TRACE = (
    "Traceback (most recent call last):\n"
    '  File "app.py", line 12, in main\n'
    "ConnectionError: cannot connect to the database"
)
_FENCED = "```python\nprint('hello')\n```"


def _utterance(index: int, text: str) -> NormalizedUtterance:
    return NormalizedUtterance(
        utterance_id=f"u-{index:03d}",
        source_adapter="claude_code",
        adapter_version="1.0.0",
        session_hash="b" * 64,
        timestamp=datetime(2026, 8, 1, 12, index, tzinfo=UTC),
        text=text,
        modality=Modality.WRITTEN,
        text_status=TextStatus.VERBATIM,
        authorship_confidence=0.9,
        authorship_basis="explicit_user_role",
        source_path_hash="c" * 64,
    )


def _write_manifest(runs_root: Path, *, provider_transfer_consent: bool) -> None:
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
            provider_transfer_confirmed_at=moment if provider_transfer_consent else None,
        ),
        stages=empty_stage_map(),
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


def _seed(runs_root: Path, *, provider_transfer_consent: bool = True) -> None:
    _write_manifest(runs_root, provider_transfer_consent=provider_transfer_consent)
    candidates_dir = stage_dir(_RUN, StageId.CANDIDATE_UTTERANCES, root=runs_root)
    candidates_dir.mkdir(parents=True)
    write_jsonl_models(
        candidates_dir / "candidates.jsonl",
        [
            _utterance(1, _PLAIN),
            _utterance(2, _MIXED),
            _utterance(3, _TRACE),
            _utterance(4, _FENCED),
        ],
    )


def _write_decisions(runs_root: Path, rows: list[dict[str, object]], *, index: int = 0) -> Path:
    directory = decisions_dir(_RUN, runs_root=runs_root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"decisions-{index:04d}.jsonl"
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8"
    )
    return path


def _candidate_text(runs_root: Path, utterance_id: str) -> str:
    for path in sorted(batch_dir(_RUN, runs_root=runs_root).glob("batch-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row["utterance_id"] == utterance_id:
                text: str = row["text"]
                return text
    raise AssertionError(f"{utterance_id} is not a candidate")


def _retain(utterance_id: str, text: str) -> dict[str, object]:
    return {
        "utterance_id": utterance_id,
        "decision": "retain",
        "retained_spans": [text],
        "reason": None,
    }


def _corpus(runs_root: Path) -> list[NormalizedUtterance]:
    corpus_path = stage_dir(_RUN, StageId.ELIGIBLE_ENGLISH, root=runs_root) / "corpus.jsonl"
    return list(read_jsonl_models(corpus_path, NormalizedUtterance))


def _all_retained(runs_root: Path) -> list[dict[str, object]]:
    return [
        _retain(candidate, _candidate_text(runs_root, candidate))
        for candidate in ("u-001", "u-002", "u-003")
    ]


# --- the batch driver ------------------------------------------------------


def test_batching_refuses_a_run_without_provider_transfer_consent(tmp_path: Path) -> None:
    _seed(tmp_path, provider_transfer_consent=False)
    with pytest.raises(MissingConsentError):
        prepare_authorship_batches(_RUN, runs_root=tmp_path)
    assert not batch_dir(_RUN, runs_root=tmp_path).exists()


def test_batching_refuses_a_run_with_no_manifest_at_all(tmp_path: Path) -> None:
    candidates_dir = stage_dir(_RUN, StageId.CANDIDATE_UTTERANCES, root=tmp_path)
    candidates_dir.mkdir(parents=True)
    write_jsonl_models(candidates_dir / "candidates.jsonl", [_utterance(1, _PLAIN)])
    with pytest.raises(RunStoreError):
        prepare_authorship_batches(_RUN, runs_root=tmp_path)
    assert not batch_dir(_RUN, runs_root=tmp_path).exists()


def test_batches_carry_pre_filtered_candidates_in_the_skill_shape(tmp_path: Path) -> None:
    _seed(tmp_path)
    index = prepare_authorship_batches(_RUN, batch_size=2, runs_root=tmp_path)

    # The fenced-only utterance is emptied by the pre-filter and never batched.
    assert index.input_count == 4
    assert index.candidate_count == 3
    assert index.dropped_empty_count == 1
    assert len(index.batches) == 2
    assert index.word_count == sum(entry.word_count for entry in index.batches)

    files = sorted(batch_dir(_RUN, runs_root=tmp_path).glob("batch-*.jsonl"))
    assert [path.name for path in files] == ["batch-0000.jsonl", "batch-0001.jsonl"]
    rows = [json.loads(line) for line in files[0].read_text().splitlines()]
    assert set(rows[0]) == {
        "utterance_id",
        "text",
        "source_adapter",
        "modality",
        "content_flags",
    }
    assert rows[0]["modality"] == "written"
    assert (batch_dir(_RUN, runs_root=tmp_path) / INDEX_NAME).is_file()


def test_batch_candidate_text_keeps_arguable_material_for_the_model(tmp_path: Path) -> None:
    _seed(tmp_path)
    prepare_authorship_batches(_RUN, runs_root=tmp_path)
    # Machinery removed, everything a model must judge kept.
    assert _candidate_text(tmp_path, "u-002") == _MIXED
    assert _candidate_text(tmp_path, "u-003") == "ConnectionError: cannot connect to the database"


def test_rerunning_the_batch_driver_replaces_stale_batches(tmp_path: Path) -> None:
    _seed(tmp_path)
    prepare_authorship_batches(_RUN, batch_size=1, runs_root=tmp_path)
    assert len(list(batch_dir(_RUN, runs_root=tmp_path).glob("batch-*.jsonl"))) == 3
    index = prepare_authorship_batches(_RUN, batch_size=25, runs_root=tmp_path)
    assert len(list(batch_dir(_RUN, runs_root=tmp_path).glob("batch-*.jsonl"))) == 1
    assert index.candidate_count == 3


def test_batch_driver_rejects_a_zero_batch_size(tmp_path: Path) -> None:
    _seed(tmp_path)
    with pytest.raises(ValueError, match="at least 1"):
        prepare_authorship_batches(_RUN, batch_size=0, runs_root=tmp_path)


# --- the deterministic verifier -------------------------------------------


def test_full_retention_builds_a_verified_corpus(tmp_path: Path) -> None:
    _seed(tmp_path)
    prepare_authorship_batches(_RUN, runs_root=tmp_path)
    _write_decisions(tmp_path, _all_retained(tmp_path))

    result = apply_authorship(_RUN, runs_root=tmp_path)
    assert result.candidates_in == 3
    assert result.retained == 3
    assert result.quarantined_decisions == 0
    assert result.missing_decisions == 0
    assert result.diagnostics == []
    assert result.words_after == result.words_before
    assert verify_corpus(_RUN, runs_root=tmp_path) == []


def test_partial_retention_keeps_exactly_the_joined_spans(tmp_path: Path) -> None:
    _seed(tmp_path)
    prepare_authorship_batches(_RUN, runs_root=tmp_path)
    _write_decisions(
        tmp_path,
        [
            _retain("u-001", _PLAIN),
            {
                "utterance_id": "u-002",
                "decision": "partial",
                "retained_spans": ["fix those issues"],
                "reason": "AUTHORSHIP_TOOL_OUTPUT",
            },
            {
                "utterance_id": "u-003",
                "decision": "exclude",
                "retained_spans": [],
                "reason": "AUTHORSHIP_TOOL_OUTPUT",
            },
        ],
    )

    result = apply_authorship(_RUN, runs_root=tmp_path)
    assert (result.retained, result.partial, result.excluded) == (1, 1, 1)
    corpus = {u.utterance_id: u.text for u in _corpus(tmp_path)}
    assert corpus["u-002"] == "fix those issues"
    # The excluded utterance contributes no text and therefore no words.
    assert "u-003" not in corpus
    assert result.words_after == count_words(_PLAIN) + count_words("fix those issues")
    assert result.words_after < result.words_before
    assert result.manifest.english_word_count == result.words_after
    assert verify_corpus(_RUN, runs_root=tmp_path) == []


def test_two_spans_join_with_a_single_newline(tmp_path: Path) -> None:
    _seed(tmp_path)
    prepare_authorship_batches(_RUN, runs_root=tmp_path)
    _write_decisions(
        tmp_path,
        [
            {
                "utterance_id": "u-002",
                "decision": "partial",
                "retained_spans": ["fix those issues", "assigned but never used"],
                "reason": "AUTHORSHIP_TOOL_OUTPUT",
            }
        ],
    )
    result = apply_authorship(_RUN, runs_root=tmp_path)
    corpus = {u.utterance_id: u.text for u in _corpus(tmp_path)}
    assert corpus["u-002"] == "fix those issues\nassigned but never used"
    assert result.partial == 1


def test_word_count_covers_retained_text_only(tmp_path: Path) -> None:
    _seed(tmp_path)
    prepare_authorship_batches(_RUN, runs_root=tmp_path)
    _write_decisions(
        tmp_path,
        [
            {
                "utterance_id": "u-002",
                "decision": "partial",
                "retained_spans": ["fix those issues"],
                "reason": "AUTHORSHIP_TOOL_OUTPUT",
            }
        ],
    )
    result = apply_authorship(_RUN, runs_root=tmp_path)
    assert result.words_after == count_words("fix those issues") == 3
    # The pasted lint line the model dropped never reaches the denominator.
    assert count_words(_MIXED) > result.words_after


@pytest.mark.parametrize(
    ("row", "code"),
    [
        (
            {
                "utterance_id": "u-999",
                "decision": "retain",
                "retained_spans": ["anything"],
                "reason": None,
            },
            "AUTHORSHIP_UNKNOWN_UTTERANCE",
        ),
        (
            {
                "utterance_id": "u-002",
                "decision": "partial",
                "retained_spans": ["fix these issues"],
                "reason": "AUTHORSHIP_TOOL_OUTPUT",
            },
            "AUTHORSHIP_SPAN_NOT_VERBATIM",
        ),
        (
            {
                "utterance_id": "u-002",
                "decision": "partial",
                "retained_spans": ["fix those issues", "those issues"],
                "reason": "AUTHORSHIP_TOOL_OUTPUT",
            },
            "AUTHORSHIP_SPAN_ORDER_INVALID",
        ),
        (
            {
                "utterance_id": "u-002",
                "decision": "partial",
                "retained_spans": ["npm run lint", "fix those issues"],
                "reason": "AUTHORSHIP_TOOL_OUTPUT",
            },
            "AUTHORSHIP_SPAN_ORDER_INVALID",
        ),
        (
            {
                "utterance_id": "u-002",
                "decision": "exclude",
                "retained_spans": ["fix those issues"],
                "reason": "AUTHORSHIP_TOOL_OUTPUT",
            },
            "SCHEMA_INVALID_VALUE",
        ),
        (
            {
                "utterance_id": "u-002",
                "decision": "partial",
                "retained_spans": ["fix those issues"],
                "reason": "AUTHORSHIP_MADE_UP",
            },
            "SCHEMA_INVALID_VALUE",
        ),
        (
            {
                "utterance_id": "u-001",
                "decision": "retain",
                "retained_spans": ["I am agree"],
                "reason": None,
            },
            "SCHEMA_INVALID_VALUE",
        ),
        (
            {"utterance_id": "u-001", "decision": "retain", "retained_spans": [_PLAIN]},
            "SCHEMA_MISSING_FIELD",
        ),
        (
            {
                "utterance_id": "u-001",
                "decision": "keep",
                "retained_spans": [_PLAIN],
                "reason": None,
            },
            "SCHEMA_INVALID_VALUE",
        ),
        (
            {
                "utterance_id": "u-001",
                "decision": "retain",
                "retained_spans": [_PLAIN],
                "reason": None,
                "confidence": 0.9,
            },
            "SCHEMA_UNEXPECTED_FIELD",
        ),
    ],
)
def test_a_failing_decision_is_quarantined(
    tmp_path: Path, row: dict[str, object], code: str
) -> None:
    _seed(tmp_path)
    prepare_authorship_batches(_RUN, runs_root=tmp_path)
    _write_decisions(tmp_path, [row])

    result = apply_authorship(_RUN, runs_root=tmp_path)
    assert code in {diagnostic.code for diagnostic in result.diagnostics}
    assert result.quarantined_decisions == 1
    assert result.manifest.utterance_count == 0
    assert result.words_after == 0
    assert verify_corpus(_RUN, runs_root=tmp_path) == []


def test_a_repeated_utterance_id_is_quarantined_once(tmp_path: Path) -> None:
    _seed(tmp_path)
    prepare_authorship_batches(_RUN, runs_root=tmp_path)
    _write_decisions(tmp_path, [_retain("u-001", _PLAIN), _retain("u-001", _PLAIN)])

    result = apply_authorship(_RUN, runs_root=tmp_path)
    codes = [diagnostic.code for diagnostic in result.diagnostics]
    assert codes.count("AUTHORSHIP_DUPLICATE_DECISION") == 1
    assert result.retained == 1
    assert result.manifest.utterance_count == 1


def test_a_malformed_line_is_reported_and_the_rest_survives(tmp_path: Path) -> None:
    _seed(tmp_path)
    prepare_authorship_batches(_RUN, runs_root=tmp_path)
    directory = decisions_dir(_RUN, runs_root=tmp_path)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "decisions-0000.jsonl").write_text(
        "{not json\n" + json.dumps(_retain("u-001", _PLAIN)) + "\n", encoding="utf-8"
    )
    result = apply_authorship(_RUN, runs_root=tmp_path)
    assert "SCHEMA_INVALID_JSON" in {diagnostic.code for diagnostic in result.diagnostics}
    assert result.retained == 1


def test_candidates_without_a_decision_are_reported(tmp_path: Path) -> None:
    _seed(tmp_path)
    prepare_authorship_batches(_RUN, runs_root=tmp_path)
    _write_decisions(tmp_path, [_retain("u-001", _PLAIN)])

    result = apply_authorship(_RUN, runs_root=tmp_path)
    assert result.missing_decisions == 2
    missing = {d.item_ref for d in result.diagnostics if d.code == "CARDINALITY_MISMATCH"}
    assert missing == {"u-002", "u-003"}


def test_missing_decision_file_is_an_error(tmp_path: Path) -> None:
    _seed(tmp_path)
    prepare_authorship_batches(_RUN, runs_root=tmp_path)
    with pytest.raises(FileNotFoundError):
        apply_authorship(_RUN, runs_root=tmp_path)


def test_decisions_are_read_from_every_batch_file_in_order(tmp_path: Path) -> None:
    _seed(tmp_path)
    prepare_authorship_batches(_RUN, batch_size=1, runs_root=tmp_path)
    _write_decisions(tmp_path, [_retain("u-001", _PLAIN)], index=0)
    _write_decisions(tmp_path, [_retain("u-002", _MIXED)], index=1)
    _write_decisions(tmp_path, [_retain("u-003", _candidate_text(tmp_path, "u-003"))], index=2)

    result = apply_authorship(_RUN, runs_root=tmp_path)
    assert result.decisions_read == 3
    assert result.retained == 3
    assert result.missing_decisions == 0


def test_reordered_decisions_produce_identical_output(tmp_path: Path) -> None:
    _seed(tmp_path)
    prepare_authorship_batches(_RUN, runs_root=tmp_path)
    rows = _all_retained(tmp_path)

    _write_decisions(tmp_path, rows)
    first = apply_authorship(_RUN, runs_root=tmp_path)
    corpus_path = stage_dir(_RUN, StageId.ELIGIBLE_ENGLISH, root=tmp_path) / "corpus.jsonl"
    first_bytes = corpus_path.read_bytes()

    _write_decisions(tmp_path, list(reversed(rows)))
    second = apply_authorship(_RUN, runs_root=tmp_path)

    assert corpus_path.read_bytes() == first_bytes
    assert second.manifest.jsonl_sha256 == first.manifest.jsonl_sha256
    assert second.words_after == first.words_after
    assert second.manifest.utterance_count == first.manifest.utterance_count


def test_decisions_directory_name_is_stable(tmp_path: Path) -> None:
    assert decisions_dir(_RUN, runs_root=tmp_path).name == DECISIONS_DIR_NAME


def test_adapter_flags_reach_the_authorship_judge(tmp_path: Path) -> None:
    """Every adapter produced content flags and nothing consumed them.

    `possible_paste` is an adapter saying "this looks like something the user
    pasted", which is the exact question stage 3 answers. Withholding it from
    the judge threw away the one piece of evidence the reader could not
    recover from the text alone.
    """
    _seed(tmp_path)
    candidates_dir = stage_dir(_RUN, StageId.CANDIDATE_UTTERANCES, root=tmp_path)
    flagged = _utterance(1, _PLAIN).model_copy(update={"content_flags": ["possible_paste"]})
    write_jsonl_models(candidates_dir / "candidates.jsonl", [flagged, _utterance(2, _MIXED)])

    prepare_authorship_batches(_RUN, batch_size=5, runs_root=tmp_path)
    rows = [
        json.loads(line)
        for path in sorted(batch_dir(_RUN, runs_root=tmp_path).glob("batch-*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    by_id = {row["utterance_id"]: row for row in rows}
    assert by_id["u-001"]["content_flags"] == ["possible_paste"]
    assert by_id["u-002"]["content_flags"] == []


def test_the_flags_carry_no_source_text(tmp_path: Path) -> None:
    # They are a fixed vocabulary the adapters define. If a flag could carry
    # free text, this projection would become a second channel for the user's
    # own words into a model's context.
    _seed(tmp_path)
    prepare_authorship_batches(_RUN, batch_size=5, runs_root=tmp_path)
    for path in sorted(batch_dir(_RUN, runs_root=tmp_path).glob("batch-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            for flag in json.loads(line)["content_flags"]:
                assert flag.replace("_", "").isalnum(), flag


def test_a_failed_judgment_can_be_asked_again(tmp_path: Path) -> None:
    """Specification 6.4 allows a bounded repair, and there was none.

    apply_authorship named the failed utterances in a file and nothing read it,
    so a quarantined judgment was listed and then lost — its words left the
    denominator with no way to get them back short of redoing the run.
    """
    _seed(tmp_path)
    prepare_authorship_batches(_RUN, batch_size=5, runs_root=tmp_path)

    # One decision claims a span the candidate does not contain, which is the
    # failure the span verifier exists to catch.
    rows = _all_retained(tmp_path)
    rows[0] = {**rows[0], "retained_spans": ["a sentence the model invented"]}
    _write_decisions(tmp_path, rows)
    result = apply_authorship(_RUN, runs_root=tmp_path)
    assert result.quarantined_decisions == 1

    repair = prepare_authorship_batches(_RUN, batch_size=5, runs_root=tmp_path, repair_only=True)
    assert repair.candidate_count == 1
    batched = [
        json.loads(line)["utterance_id"]
        for path in sorted(batch_dir(_RUN, runs_root=tmp_path).glob("batch-*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert batched == ["u-001"]


def test_a_repair_with_nothing_to_repair_is_refused(tmp_path: Path) -> None:
    # Writing an empty batch would send the skill to read nothing and report a
    # clean zero, which is the shape of failure this project keeps finding.
    _seed(tmp_path)
    prepare_authorship_batches(_RUN, batch_size=5, runs_root=tmp_path)
    _write_decisions(tmp_path, _all_retained(tmp_path))
    apply_authorship(_RUN, runs_root=tmp_path)
    with pytest.raises(ValueError, match="nothing is listed for repair"):
        prepare_authorship_batches(_RUN, batch_size=5, runs_root=tmp_path, repair_only=True)

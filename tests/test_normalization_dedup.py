"""Cross-source exact and fuzzy deduplication (specification, 4.8)."""

import difflib
import random
from datetime import UTC, datetime, timedelta

from glite_english_audit.artifacts.enums import Modality, TextStatus
from glite_english_audit.artifacts.models import NormalizedUtterance
from glite_english_audit.normalization.dedup import (
    FUZZY_RATIO_THRESHOLD,
    PRODUCER_VERSION,
    TEMPORAL_PROXIMITY_LIMIT,
    dedupe,
)

_T0 = datetime(2026, 1, 5, 12, 0, 0, tzinfo=UTC)


def _utt(
    uid: str,
    text: str,
    *,
    adapter: str = "claude_code",
    ts: datetime | None = None,
    modality: Modality = Modality.WRITTEN,
    status: TextStatus = TextStatus.VERBATIM,
    path_hash: str = "path-hash-a",
) -> NormalizedUtterance:
    return NormalizedUtterance(
        utterance_id=uid,
        source_adapter=adapter,
        adapter_version="1.0.0",
        session_hash="session-1",
        timestamp=ts,
        text=text,
        modality=modality,
        text_status=status,
        authorship_confidence=1.0,
        authorship_basis="role=user",
        source_path_hash=path_hash,
    )


def test_producer_version_is_semver() -> None:
    parts = PRODUCER_VERSION.split(".")
    assert len(parts) == 3
    assert all(part.isdigit() for part in parts)


def test_documented_thresholds() -> None:
    assert FUZZY_RATIO_THRESHOLD == 0.92
    assert timedelta(minutes=10) == TEMPORAL_PROXIMITY_LIMIT


def test_no_duplicates_all_kept() -> None:
    utterances = [
        _utt("u1", "please fix the login bug", ts=_T0),
        _utt("u2", "now update the changelog", ts=_T0 + timedelta(minutes=1)),
    ]
    outcome = dedupe(utterances)
    assert [u.utterance_id for u in outcome.canonical] == ["u1", "u2"]
    assert outcome.excluded == []


def test_exact_cross_adapter_collapsed() -> None:
    utterances = [
        _utt("u1", "Fix the  Login bug", adapter="codex", ts=_T0),
        _utt("u2", "fix the login bug", adapter="claude_code", ts=_T0 + timedelta(minutes=2)),
    ]
    outcome = dedupe(utterances)
    assert [u.utterance_id for u in outcome.canonical] == ["u1"]
    assert len(outcome.excluded) == 1
    exclusion = outcome.excluded[0]
    assert exclusion.excluded_utterance_id == "u2"
    assert exclusion.canonical_utterance_id == "u1"
    assert exclusion.kind == "exact"


def test_dictated_then_pasted_keeps_wispr_original() -> None:
    text = "please refactor the settings page and add a dark mode toggle"
    wispr = _utt(
        "wispr-1",
        text,
        adapter="wispr_flow",
        ts=_T0,
        modality=Modality.SPOKEN_ASR,
        status=TextStatus.VERBATIM,
        path_hash="path-hash-wispr",
    )
    pasted = _utt(
        "claude-1",
        text,
        adapter="claude_code",
        ts=_T0 + timedelta(seconds=30),
        modality=Modality.WRITTEN,
        status=TextStatus.VERBATIM,
    )
    outcome = dedupe([pasted, wispr])
    assert [u.utterance_id for u in outcome.canonical] == ["wispr-1"]
    assert outcome.excluded[0].excluded_utterance_id == "claude-1"
    assert outcome.excluded[0].canonical_utterance_id == "wispr-1"


def test_repeated_phrase_days_apart_preserved() -> None:
    utterances = [
        _utt("u1", "run the tests again", adapter="codex", ts=_T0),
        _utt("u2", "run the tests again", adapter="claude_code", ts=_T0 + timedelta(days=3)),
    ]
    outcome = dedupe(utterances)
    assert [u.utterance_id for u in outcome.canonical] == ["u1", "u2"]
    assert outcome.excluded == []


def test_ten_minute_boundary_is_inclusive() -> None:
    at_limit = dedupe(
        [
            _utt("u1", "deploy the staging build", adapter="codex", ts=_T0),
            _utt(
                "u2",
                "deploy the staging build",
                adapter="claude_code",
                ts=_T0 + timedelta(minutes=10),
            ),
        ]
    )
    assert [u.utterance_id for u in at_limit.canonical] == ["u1"]

    past_limit = dedupe(
        [
            _utt("u3", "deploy the staging build", adapter="codex", ts=_T0),
            _utt(
                "u4",
                "deploy the staging build",
                adapter="claude_code",
                ts=_T0 + timedelta(minutes=10, seconds=1),
            ),
        ]
    )
    assert [u.utterance_id for u in past_limit.canonical] == ["u3", "u4"]
    assert past_limit.excluded == []


def test_fuzzy_ratio_boundary() -> None:
    base = "abcdefghijklmnopqrstuvwxy"
    at_threshold = "abcdefghijklmnopqrstuvwzz"
    below_threshold = "abcdefghijklmnopqrstuvzzz"
    ratio_at = difflib.SequenceMatcher(a=base, b=at_threshold, autojunk=False).ratio()
    ratio_below = difflib.SequenceMatcher(a=base, b=below_threshold, autojunk=False).ratio()
    assert ratio_at == FUZZY_RATIO_THRESHOLD
    assert ratio_below < FUZZY_RATIO_THRESHOLD

    collapsed = dedupe(
        [
            _utt("u1", base, adapter="wispr_flow", ts=_T0, modality=Modality.SPOKEN_ASR),
            _utt("u2", at_threshold, adapter="claude_code", ts=_T0 + timedelta(minutes=1)),
        ]
    )
    assert [u.utterance_id for u in collapsed.canonical] == ["u1"]
    assert collapsed.excluded[0].kind == "fuzzy"

    kept = dedupe(
        [
            _utt("u3", base, adapter="wispr_flow", ts=_T0, modality=Modality.SPOKEN_ASR),
            _utt("u4", below_threshold, adapter="claude_code", ts=_T0 + timedelta(minutes=1)),
        ]
    )
    assert [u.utterance_id for u in kept.canonical] == ["u3", "u4"]


def test_missing_timestamp_allows_exact_only() -> None:
    exact = dedupe(
        [
            _utt("u1", "check the failing pipeline", adapter="codex", ts=_T0),
            _utt("u2", "check the failing pipeline", adapter="claude_code", ts=None),
        ]
    )
    assert [u.utterance_id for u in exact.canonical] == ["u1"]
    assert exact.excluded[0].kind == "exact"

    fuzzy_only = dedupe(
        [
            _utt("u3", "abcdefghijklmnopqrstuvwxy", adapter="codex", ts=_T0),
            _utt("u4", "abcdefghijklmnopqrstuvwzz", adapter="claude_code", ts=None),
        ]
    )
    assert [u.utterance_id for u in fuzzy_only.canonical] == ["u3", "u4"]
    assert fuzzy_only.excluded == []


def test_same_adapter_same_instance_never_collapsed() -> None:
    outcome = dedupe(
        [
            _utt("u1", "restart the worker", ts=_T0),
            _utt("u2", "restart the worker", ts=_T0 + timedelta(minutes=1)),
        ]
    )
    assert [u.utterance_id for u in outcome.canonical] == ["u1", "u2"]
    assert outcome.excluded == []


def test_same_adapter_different_instance_collapsed() -> None:
    outcome = dedupe(
        [
            _utt("u1", "restart the worker", ts=_T0, path_hash="path-hash-a"),
            _utt("u2", "restart the worker", ts=_T0 + timedelta(minutes=1), path_hash="path-b"),
        ]
    )
    assert [u.utterance_id for u in outcome.canonical] == ["u1"]
    assert outcome.excluded[0].excluded_utterance_id == "u2"


def test_three_way_copy_yields_one_canonical() -> None:
    text = "add a retry to the upload endpoint"
    outcome = dedupe(
        [
            _utt("codex-1", text, adapter="codex", ts=_T0 + timedelta(minutes=1)),
            _utt(
                "wispr-1",
                text,
                adapter="wispr_flow",
                ts=_T0,
                modality=Modality.SPOKEN_ASR,
                path_hash="path-hash-wispr",
            ),
            _utt("claude-1", text, adapter="claude_code", ts=_T0 + timedelta(seconds=30)),
        ]
    )
    assert [u.utterance_id for u in outcome.canonical] == ["wispr-1"]
    assert {e.excluded_utterance_id for e in outcome.excluded} == {"codex-1", "claude-1"}
    assert all(e.canonical_utterance_id == "wispr-1" for e in outcome.excluded)


def test_verbatim_preferred_over_cleaned() -> None:
    outcome = dedupe(
        [
            _utt(
                "cleaned-1",
                "merge the feature branch",
                adapter="codex",
                ts=_T0,
                status=TextStatus.CLEANED,
            ),
            _utt(
                "verbatim-1",
                "merge the feature branch",
                adapter="claude_code",
                ts=_T0 + timedelta(minutes=1),
            ),
        ]
    )
    assert [u.utterance_id for u in outcome.canonical] == ["verbatim-1"]


def test_output_order_is_timestamp_then_id() -> None:
    outcome = dedupe(
        [
            _utt("z-late", "third message entirely", ts=_T0 + timedelta(hours=2)),
            _utt("b-none", "message without a timestamp", ts=None),
            _utt("a-early", "first message entirely", ts=_T0),
        ]
    )
    assert [u.utterance_id for u in outcome.canonical] == ["a-early", "z-late", "b-none"]


def test_deterministic_under_shuffle() -> None:
    text = "ship the release notes to the blog"
    utterances = [
        _utt("wispr-1", text, adapter="wispr_flow", ts=_T0, modality=Modality.SPOKEN_ASR),
        _utt("claude-1", text, adapter="claude_code", ts=_T0 + timedelta(seconds=45)),
        _utt("codex-1", "unrelated message about the parser", adapter="codex", ts=_T0),
        _utt("late-1", text, adapter="codex", ts=_T0 + timedelta(days=2)),
        _utt("none-1", "another standalone thought", ts=None),
    ]
    baseline = dedupe(list(utterances))
    for seed in range(5):
        shuffled = list(utterances)
        random.Random(seed).shuffle(shuffled)
        outcome = dedupe(shuffled)
        assert outcome == baseline
    assert [u.utterance_id for u in baseline.canonical] == [
        "codex-1",
        "wispr-1",
        "late-1",
        "none-1",
    ]
    assert [e.excluded_utterance_id for e in baseline.excluded] == ["claude-1"]

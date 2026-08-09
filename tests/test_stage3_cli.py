"""Tests for the stage-3 filter and verifier CLI modules."""

import json
from datetime import UTC, datetime
from pathlib import Path

from glite_english_audit.artifacts.enums import Modality, StepId, TextStatus
from glite_english_audit.artifacts.io import read_model, write_jsonl_models
from glite_english_audit.artifacts.models import EligibleCorpusManifest, NormalizedUtterance
from glite_english_audit.normalization.filter_corpus import filter_corpus
from glite_english_audit.paths import step_dir
from glite_english_audit.verification.verify_corpus import verify_corpus

_RUN = "run-" + "1" * 32


def _utterance(index: int, text: str, *, path_hash: str = "c" * 64) -> NormalizedUtterance:
    return NormalizedUtterance(
        utterance_id=f"u-{index:03d}",
        source_adapter="claude_code",
        adapter_version="1.0.0",
        session_hash="b" * 64,
        timestamp=datetime(2026, 8, 1, 12, index % 60, tzinfo=UTC),
        text=text,
        modality=Modality.WRITTEN,
        text_status=TextStatus.VERBATIM,
        authorship_confidence=0.9,
        authorship_basis="explicit_user_role",
        source_path_hash=path_hash,
    )


def _seed_candidates(runs_root: Path) -> None:
    candidates_dir = step_dir(_RUN, StepId.A_COLLECTED, root=runs_root)
    candidates_dir.mkdir(parents=True)
    rows = [
        _utterance(1, "Yesterday I very like this new plan and want to continue"),
        # The same text seconds later from another instance: a cross-instance
        # copy of one production event, so dedup must collapse it.
        _utterance(
            2, "Yesterday I very like this new plan and want to continue", path_hash="d" * 64
        ),
        _utterance(3, "```\ndef broken():\n    pass\n```"),  # code only -> dropped
        _utterance(4, "привет мир это тест на русском языке без английского"),  # quarantined
        _utterance(5, "Please check the second draft because the wording sounds off"),
    ]
    write_jsonl_models(candidates_dir / "candidates.jsonl", rows)


def test_filter_corpus_produces_verified_manifest(tmp_path: Path) -> None:
    _seed_candidates(tmp_path)
    manifest = filter_corpus(_RUN, runs_root=tmp_path)
    assert manifest.utterance_count == 2
    assert manifest.deduplicated_utterance_count == 1
    assert manifest.quarantined_utterance_count == 2
    assert manifest.english_word_count > 0
    assert verify_corpus(_RUN, runs_root=tmp_path) == []


def test_verify_corpus_detects_tampered_bytes(tmp_path: Path) -> None:
    _seed_candidates(tmp_path)
    filter_corpus(_RUN, runs_root=tmp_path)
    out_dir = step_dir(_RUN, StepId.C_AUTHORED, root=tmp_path)
    corpus = out_dir / "corpus.jsonl"
    corpus.write_text(corpus.read_text().replace("plan", "scheme"), encoding="utf-8")
    codes = {d.code for d in verify_corpus(_RUN, runs_root=tmp_path)}
    assert "LINEAGE_HASH_MISMATCH" in codes


def test_verify_corpus_detects_count_tampering(tmp_path: Path) -> None:
    _seed_candidates(tmp_path)
    filter_corpus(_RUN, runs_root=tmp_path)
    out_dir = step_dir(_RUN, StepId.C_AUTHORED, root=tmp_path)
    manifest_path = out_dir / "eligible-corpus-manifest.json"
    manifest = read_model(manifest_path, EligibleCorpusManifest)
    tampered = manifest.model_copy(update={"english_word_count": manifest.english_word_count + 5})
    manifest_path.write_text(
        json.dumps(tampered.model_dump(mode="json"), indent=2), encoding="utf-8"
    )
    codes = {d.code for d in verify_corpus(_RUN, runs_root=tmp_path)}
    assert "LINEAGE_HASH_MISMATCH" not in codes
    assert "ARITHMETIC_INVARIANT_VIOLATION" in codes


def test_verify_corpus_missing_manifest(tmp_path: Path) -> None:
    codes = {d.code for d in verify_corpus(_RUN, runs_root=tmp_path)}
    assert codes == {"LINEAGE_MISSING_INPUT"}

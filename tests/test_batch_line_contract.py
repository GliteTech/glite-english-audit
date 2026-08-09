"""What the batch writers emit must validate as the model the skill names.

Both semantic stages hand a model a JSONL file and a skill that tells it to
validate every line. Nothing used to compare the two: ``pipeline/batches.py``
wrote a three-field projection while ``analyze-english-text`` said the lines
were ``NormalizedUtterance``, which needs thirteen. An agent obeying the skill
skipped every line, reported clean zero counts, and told the user their English
had no mistakes -- a run that reports success while reading nothing.

These tests are the missing comparison: they parse real writer output and
validate it against the exact model each SKILL.md names, and they read the
skill file to confirm it still names that model.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from glite_english_audit.artifacts.enums import (
    AgentRuntime,
    Modality,
    OsEnvironment,
    RunStatus,
    StageId,
    TextStatus,
)
from glite_english_audit.artifacts.io import write_jsonl_models, write_model
from glite_english_audit.artifacts.manifest import (
    MANIFEST_SCHEMA_VERSION,
    CompatibilityFingerprint,
    ConsentState,
    RunManifest,
    empty_stage_map,
)
from glite_english_audit.artifacts.models import NormalizedUtterance
from glite_english_audit.consent import CONSENT_POLICY_VERSION
from glite_english_audit.normalization.tokenizer import TOKENIZER_VERSION
from glite_english_audit.paths import repo_root, stage_dir
from glite_english_audit.pipeline.authorship_batches import (
    BATCH_GLOB,
    Candidate,
    batch_dir,
    prepare_authorship_batches,
)
from glite_english_audit.pipeline.batches import AnalysisUtterance, prepare_batches
from glite_english_audit.state.run_store import RUN_MANIFEST_FILENAME

_RUN = "run-" + "3" * 32
_NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


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


def _rows(paths: list[Path]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _skill_text(slug: str) -> str:
    return (repo_root() / "skills" / slug / "SKILL.md").read_text(encoding="utf-8")


def _seed_consented_run(runs_root: Path) -> None:
    """A run allowed to prepare provider-bound text.

    Both batch writers refuse without this run's own provider-transfer consent,
    which is the point of that guard: these files exist only to be handed to an
    AI provider. The contract under test is the line shape, so the consent is
    granted here rather than worked around.
    """
    manifest = RunManifest(
        manifest_schema_version=MANIFEST_SCHEMA_VERSION,
        run_id=_RUN,
        created_at=_NOW,
        runtime=AgentRuntime.CLAUDE_CODE,
        os_environment=OsEnvironment.MACOS,
        status=RunStatus.PROCESSING,
        consent=ConsentState(
            consent_policy_version=CONSENT_POLICY_VERSION,
            local_scan_confirmed_at=_NOW,
            provider_transfer_confirmed_at=_NOW,
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
    (runs_root / _RUN).mkdir(parents=True, exist_ok=True)
    write_model(runs_root / _RUN / RUN_MANIFEST_FILENAME, manifest)


def test_stage4_batch_lines_validate_as_the_model_the_skill_names(tmp_path: Path) -> None:
    _seed_consented_run(tmp_path)
    corpus_dir = stage_dir(_RUN, StageId.ELIGIBLE_ENGLISH, root=tmp_path)
    corpus_dir.mkdir(parents=True)
    write_jsonl_models(
        corpus_dir / "corpus.jsonl",
        [
            _utterance(1, "Yesterday I have finished the report."),
            _utterance(2, "I very like this approach."),
        ],
    )
    result = prepare_batches(_RUN, batch_size=1, runs_root=tmp_path)
    rows = _rows(sorted(Path(str(result["batch_dir"])).glob("batch-*.jsonl")))

    assert len(rows) == 2
    for row in rows:
        # extra="forbid" makes this an equality check on the field set, not a
        # subset check: a widened writer fails here instead of quietly sending
        # more of the user's private record into a model's context.
        AnalysisUtterance.model_validate(row)


def test_stage3_batch_lines_validate_as_the_model_the_skill_names(tmp_path: Path) -> None:
    _seed_consented_run(tmp_path)
    candidates_dir = stage_dir(_RUN, StageId.CANDIDATE_UTTERANCES, root=tmp_path)
    candidates_dir.mkdir(parents=True)
    write_jsonl_models(
        candidates_dir / "candidates.jsonl",
        [
            _utterance(1, "Please check the second draft because the wording sounds off."),
            _utterance(2, "I am agree that the first variant reads better."),
        ],
    )
    prepare_authorship_batches(_RUN, batch_size=1, runs_root=tmp_path)
    rows = _rows(sorted(batch_dir(_RUN, runs_root=tmp_path).glob(BATCH_GLOB)))

    assert len(rows) == 2
    for row in rows:
        Candidate.model_validate(row)


def test_skills_still_name_the_models_their_batches_validate_against() -> None:
    # The failure this guards is a skill drifting back to naming a model that
    # does not match the bytes: the test above would still pass while an agent
    # following the skill skipped every line.
    analyze = _skill_text("analyze-english-text")
    assert "`AnalysisUtterance` in `src/glite_english_audit/pipeline/batches.py`" in analyze
    assert "NormalizedUtterance" not in analyze

    authored = _skill_text("filter-authored-english")
    assert "`Candidate` in `src/glite_english_audit/pipeline/authorship_batches.py`" in authored

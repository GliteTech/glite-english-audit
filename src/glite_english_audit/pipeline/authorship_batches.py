"""CLI: stage 3 — prepare candidate batches for the authorship judgment.

Run: ``uv run python -m glite_english_audit.pipeline.authorship_batches
--run-id <run-id>`` (tests pass ``--runs-root``).

Reads the stage-2 candidates, applies the deterministic pre-filter
(:mod:`glite_english_audit.normalization.authorship`), drops candidates the
pre-filter leaves empty, and writes numbered batch files in the shape the
``filter-authored-english`` skill reads, plus an index recording how many
candidates and words entered the judgment.

Batching is deterministic and resumable: the same candidates always yield the
same batches in the same order, so a rerun after an interruption reproduces
them exactly, and :mod:`glite_english_audit.pipeline.apply_authorship` can
rebuild the same candidate text when it checks the model's spans. Prints
aggregate numbers only; no candidate text reaches the conversation.
"""

import argparse
import json
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from glite_english_audit.artifacts.enums import Modality, StageId
from glite_english_audit.artifacts.io import (
    atomic_write_text,
    ensure_private_dir,
    read_jsonl_models,
    write_model,
)
from glite_english_audit.artifacts.models import NormalizedUtterance
from glite_english_audit.consent import require_provider_transfer_consent
from glite_english_audit.normalization.authorship import PRODUCER_VERSION as PREFILTER_VERSION
from glite_english_audit.normalization.authorship import strip_non_authored
from glite_english_audit.normalization.tokenizer import TOKENIZER_VERSION, count_words
from glite_english_audit.paths import stage_dir

CANDIDATES_NAME = "candidates.jsonl"
BATCH_DIR_NAME = "candidate-batches"
BATCH_GLOB = "batch-*.jsonl"
INDEX_NAME = "candidate-batch-index.json"
DECISIONS_DIR_NAME = "decisions"
DEFAULT_BATCH_SIZE = 25


class CandidateBatchEntry(BaseModel):
    """One written batch file and what entered it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    file_name: str
    candidate_count: int = Field(ge=0)
    word_count: int = Field(ge=0)


class CandidateBatchIndex(BaseModel):
    """What the pre-filter handed to the model, in counts only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    prefilter_version: str
    tokenizer_version: str
    batch_size: int = Field(ge=1)
    candidate_count: int = Field(ge=0)
    word_count: int = Field(ge=0)
    input_count: int = Field(ge=0)
    dropped_empty_count: int = Field(ge=0)
    batches: list[CandidateBatchEntry] = Field(default_factory=list)


class Candidate(BaseModel):
    """One pre-filtered candidate as the skill reads it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    utterance_id: str
    text: str
    source_adapter: str
    modality: Modality


def build_candidates(utterances: list[NormalizedUtterance]) -> list[Candidate]:
    """Pre-filter ``utterances`` and drop the ones left with no text.

    The same function backs the batch files and the span check in
    ``apply_authorship``, so both see byte-identical candidate text.
    """
    candidates: list[Candidate] = []
    for utterance in utterances:
        text = strip_non_authored(utterance.text).cleaned_text.strip()
        if not text:
            continue
        candidates.append(
            Candidate(
                utterance_id=utterance.utterance_id,
                text=text,
                source_adapter=utterance.source_adapter,
                modality=utterance.modality,
            )
        )
    return candidates


def read_candidate_utterances(
    run_id: str, *, runs_root: Path | None = None
) -> list[NormalizedUtterance]:
    """Read one run's stage-2 candidate utterances, unfiltered."""
    candidates_path = (
        stage_dir(run_id, StageId.CANDIDATE_UTTERANCES, root=runs_root) / CANDIDATES_NAME
    )
    return list(read_jsonl_models(candidates_path, NormalizedUtterance))


def batch_dir(run_id: str, *, runs_root: Path | None = None) -> Path:
    """Directory holding this run's candidate batches and their index."""
    return stage_dir(run_id, StageId.ELIGIBLE_ENGLISH, root=runs_root) / BATCH_DIR_NAME


def decisions_dir(run_id: str, *, runs_root: Path | None = None) -> Path:
    """Directory the skill writes one decisions file per batch into."""
    return stage_dir(run_id, StageId.ELIGIBLE_ENGLISH, root=runs_root) / DECISIONS_DIR_NAME


def prepare_authorship_batches(
    run_id: str, *, batch_size: int = DEFAULT_BATCH_SIZE, runs_root: Path | None = None
) -> CandidateBatchIndex:
    """Split this run's pre-filtered candidates into numbered batch files."""
    if batch_size < 1:
        msg = "batch size must be at least 1"
        raise ValueError(msg)
    # Batch files exist only to be read by the model, so writing one is the
    # moment the learner's sentences become provider-bound.
    require_provider_transfer_consent(run_id, runs_root=runs_root)
    utterances = read_candidate_utterances(run_id, runs_root=runs_root)
    candidates = build_candidates(utterances)

    out_dir = ensure_private_dir(batch_dir(run_id, runs_root=runs_root))
    # The skill writes its decisions here; creating the directory now is what
    # makes it owner-only, since the skill's own writer sets no mode.
    ensure_private_dir(decisions_dir(run_id, runs_root=runs_root))
    for stale in out_dir.glob(BATCH_GLOB):
        stale.unlink()

    entries: list[CandidateBatchEntry] = []
    for index in range(0, len(candidates), batch_size):
        chunk = candidates[index : index + batch_size]
        path = out_dir / f"batch-{index // batch_size:04d}.jsonl"
        atomic_write_text(
            path,
            "\n".join(
                json.dumps(candidate.model_dump(mode="json"), ensure_ascii=False)
                for candidate in chunk
            )
            + "\n",
        )
        entries.append(
            CandidateBatchEntry(
                file_name=path.name,
                candidate_count=len(chunk),
                word_count=sum(count_words(candidate.text) for candidate in chunk),
            )
        )

    index_model = CandidateBatchIndex(
        prefilter_version=PREFILTER_VERSION,
        tokenizer_version=TOKENIZER_VERSION,
        batch_size=batch_size,
        candidate_count=len(candidates),
        word_count=sum(entry.word_count for entry in entries),
        input_count=len(utterances),
        dropped_empty_count=len(utterances) - len(candidates),
        batches=entries,
    )
    write_model(out_dir / INDEX_NAME, index_model)
    return index_model


def main(argv: list[str] | None = None) -> int:
    """CLI entry point printing aggregate counts as JSON."""
    parser = argparse.ArgumentParser(description="Stage 3: prepare authorship candidate batches")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--runs-root", type=Path, default=None, help="test override")
    arguments = parser.parse_args(argv)
    index_model = prepare_authorship_batches(
        arguments.run_id, batch_size=arguments.batch_size, runs_root=arguments.runs_root
    )
    sys.stdout.write(
        json.dumps(
            {
                "batches": len(index_model.batches),
                "batch_size": index_model.batch_size,
                "candidates": index_model.candidate_count,
                "candidate_words": index_model.word_count,
                "dropped_empty": index_model.dropped_empty_count,
                "batch_dir": str(batch_dir(arguments.run_id, runs_root=arguments.runs_root)),
                "decisions_dir": str(
                    decisions_dir(arguments.run_id, runs_root=arguments.runs_root)
                ),
            },
            indent=2,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

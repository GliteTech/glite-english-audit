"""CLI: prepare stage-4 input batches from the eligible corpus.

Run: ``uv run python -m glite_english_audit.pipeline.batches --run-id <run-id>``

The semantic producer reads batches, not the whole corpus, so batching is
deterministic and resumable: the same corpus always yields the same batches in
the same order, and a rerun after an interruption reproduces them exactly.
Batches are transport units only; the utterance stays the checkpoint unit
(specification, 9.3).

A batch line is a deliberate projection of the corpus record, not the whole
:class:`NormalizedUtterance`: session hashes, path hashes, timestamps, and
adapter versions cannot help judge English, so sending them into a model's
context spends privacy for nothing. :class:`AnalysisUtterance` names that
projection, so the ``analyze-english-text`` skill can tell an agent to validate
each line against a model that actually matches the bytes on disk.
"""

import argparse
import json
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from glite_english_audit.artifacts.enums import Modality, StageId
from glite_english_audit.artifacts.io import ensure_private_dir, read_jsonl_models
from glite_english_audit.artifacts.models import NormalizedUtterance
from glite_english_audit.normalization.tokenizer import count_words
from glite_english_audit.paths import stage_dir

CORPUS_NAME = "corpus.jsonl"
BATCH_DIR_NAME = "batches"
DEFAULT_BATCH_SIZE = 25


class AnalysisUtterance(BaseModel):
    """One eligible utterance as the ``analyze-english-text`` skill reads it.

    Exactly the three fields a stage-4 batch line carries. ``extra="forbid"``
    is the point: widening the batch writer without widening this model, or
    the reverse, fails the round-trip test instead of silently changing what
    reaches the model.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    utterance_id: str
    text: str
    modality: Modality


def prepare_batches(
    run_id: str, *, batch_size: int = DEFAULT_BATCH_SIZE, runs_root: Path | None = None
) -> dict[str, object]:
    """Split the eligible corpus into numbered batch files."""
    if batch_size < 1:
        msg = "batch size must be at least 1"
        raise ValueError(msg)
    corpus_dir = stage_dir(run_id, StageId.ELIGIBLE_ENGLISH, root=runs_root)
    corpus = list(read_jsonl_models(corpus_dir / CORPUS_NAME, NormalizedUtterance))
    batch_dir = ensure_private_dir(
        stage_dir(run_id, StageId.PLAIN_FINDINGS, root=runs_root) / BATCH_DIR_NAME
    )
    for stale in batch_dir.glob("batch-*.jsonl"):
        stale.unlink()

    written: list[str] = []
    for index in range(0, len(corpus), batch_size):
        chunk = corpus[index : index + batch_size]
        path = batch_dir / f"batch-{index // batch_size:04d}.jsonl"
        path.write_text(
            "\n".join(
                json.dumps(
                    AnalysisUtterance(
                        utterance_id=u.utterance_id, text=u.text, modality=u.modality
                    ).model_dump(mode="json"),
                    ensure_ascii=False,
                )
                for u in chunk
            )
            + "\n",
            encoding="utf-8",
        )
        written.append(path.name)
    return {
        "batches": len(written),
        "batch_size": batch_size,
        "utterances": len(corpus),
        "words": sum(count_words(u.text) for u in corpus),
        "batch_dir": str(batch_dir),
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Prepare stage-4 batches")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--runs-root", type=Path, default=None, help="test override")
    arguments = parser.parse_args(argv)
    result = prepare_batches(
        arguments.run_id, batch_size=arguments.batch_size, runs_root=arguments.runs_root
    )
    sys.stdout.write(json.dumps(result, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

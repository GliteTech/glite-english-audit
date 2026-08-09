"""CLI: stage 3 fallback — build the eligible corpus from the pre-filter alone.

Run: ``uv run python -m glite_english_audit.normalization.filter_corpus
--run-id <run-id>`` (tests pass ``--runs-root``).

This is the pre-filter-only route, used when no model judgment is available —
in tests, and when an audit has to produce a corpus without spending a model
call. The normal stage-3 path is
:mod:`glite_english_audit.pipeline.apply_authorship`, where the
``filter-authored-english`` skill decides which spans the learner wrote and
the deterministic verifier counts what it kept. Here nothing judges
authorship: the pre-filter removes only machinery
(:mod:`glite_english_audit.normalization.authorship`), so pasted material it
cannot recognize survives into the analyzed-word denominator and depresses
every rate the report shows.

Reads the stage-2 candidate JSONL, applies the pre-filter, conservative
English classification, and cross-source dedup, counts words with the
versioned tokenizer, and writes the stage-3 corpus JSONL plus its
:class:`EligibleCorpusManifest` — the same artifacts, in the same shape, as
the model path. Prints only aggregate numbers.
"""

import argparse
import json
import sys
from pathlib import Path

from glite_english_audit import CLIENT_VERSION
from glite_english_audit.artifacts.enums import StepId
from glite_english_audit.artifacts.envelope import ArtifactEnvelope, utc_now
from glite_english_audit.artifacts.hashing import new_artifact_id, sha256_hex
from glite_english_audit.artifacts.io import (
    ensure_private_dir,
    read_jsonl_models,
    write_jsonl_models,
    write_model,
)
from glite_english_audit.artifacts.models import EligibleCorpusManifest, NormalizedUtterance
from glite_english_audit.normalization.authorship import strip_non_authored
from glite_english_audit.normalization.dedup import dedupe
from glite_english_audit.normalization.language import classify_english
from glite_english_audit.normalization.tokenizer import TOKENIZER_VERSION, count_words
from glite_english_audit.paths import step_dir

CANDIDATES_NAME = "candidates.jsonl"
CORPUS_NAME = "corpus.jsonl"
MANIFEST_NAME = "eligible-corpus-manifest.json"
PRODUCER_NAME = "filter_corpus"


def filter_corpus(run_id: str, *, runs_root: Path | None = None) -> EligibleCorpusManifest:
    """Run the stage-3 filter for one run and persist its artifacts."""
    candidates_path = step_dir(run_id, StepId.A_COLLECTED, root=runs_root) / (CANDIDATES_NAME)
    out_dir = ensure_private_dir(step_dir(run_id, StepId.C_AUTHORED, root=runs_root))

    eligible: list[NormalizedUtterance] = []
    quarantined = 0
    for utterance in read_jsonl_models(candidates_path, NormalizedUtterance):
        cleaned = strip_non_authored(utterance.text).cleaned_text.strip()
        if not cleaned:
            quarantined += 1
            continue
        decision = classify_english(cleaned)
        if decision.quarantined or decision.english_text is None:
            quarantined += 1
            continue
        eligible.append(utterance.model_copy(update={"text": decision.english_text}))

    outcome = dedupe(eligible)
    corpus_path = out_dir / CORPUS_NAME
    count = write_jsonl_models(corpus_path, outcome.canonical)
    words = sum(count_words(u.text) for u in outcome.canonical)

    manifest = EligibleCorpusManifest(
        envelope=ArtifactEnvelope(
            schema_name="eligible_corpus",
            schema_version=1,
            artifact_id=new_artifact_id(),
            run_id=run_id,
            stage_id=StepId.C_AUTHORED,
            producer_name=PRODUCER_NAME,
            producer_version=CLIENT_VERSION,
            created_at=utc_now(),
        ),
        tokenizer_version=TOKENIZER_VERSION,
        utterance_count=count,
        english_word_count=words,
        quarantined_utterance_count=quarantined,
        deduplicated_utterance_count=len(outcome.excluded),
        jsonl_relative_path=CORPUS_NAME,
        jsonl_sha256=sha256_hex(corpus_path.read_bytes()),
    )
    write_model(out_dir / MANIFEST_NAME, manifest)
    return manifest


def main(argv: list[str] | None = None) -> int:
    """CLI entry point printing aggregate counts as JSON."""
    parser = argparse.ArgumentParser(description="Stage 3: filter the eligible English corpus")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--runs-root", type=Path, default=None, help="test override")
    arguments = parser.parse_args(argv)
    manifest = filter_corpus(arguments.run_id, runs_root=arguments.runs_root)
    sys.stdout.write(
        json.dumps(
            {
                "eligible_utterances": manifest.utterance_count,
                "eligible_english_words": manifest.english_word_count,
                "quarantined_utterances": manifest.quarantined_utterance_count,
                "deduplicated_utterances": manifest.deduplicated_utterance_count,
                "tokenizer_version": manifest.tokenizer_version,
            },
            indent=2,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

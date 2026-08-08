"""CLI: deterministic stage-3 corpus verifier.

Run: ``uv run python -m glite_english_audit.verification.verify_corpus
--run-id <run-id>`` (tests pass ``--runs-root``).

Checks the eligible-corpus manifest against the corpus bytes: schema, file
hash, record cardinality, tokenizer version, and an independent word recount.
Prints diagnostics; exits non-zero when any error-level diagnostic fires.
"""

import argparse
import sys
from pathlib import Path

from glite_english_audit.artifacts.enums import StageId
from glite_english_audit.artifacts.io import read_jsonl_models, read_model
from glite_english_audit.artifacts.models import EligibleCorpusManifest, NormalizedUtterance
from glite_english_audit.diagnostics.codes import Diagnostic, Severity
from glite_english_audit.normalization.tokenizer import TOKENIZER_VERSION, count_words
from glite_english_audit.paths import stage_dir
from glite_english_audit.verification.deterministic import verify_file_hash

MANIFEST_NAME = "eligible-corpus-manifest.json"


def verify_corpus(run_id: str, *, runs_root: Path | None = None) -> list[Diagnostic]:
    """Deterministic checks for one run's stage-3 output."""
    out_dir = stage_dir(run_id, StageId.ELIGIBLE_ENGLISH, root=runs_root)
    manifest_path = out_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        return [
            Diagnostic.from_code(
                "LINEAGE_MISSING_INPUT",
                "the eligible-corpus manifest does not exist for this run",
                item_ref=run_id,
            )
        ]
    try:
        manifest = read_model(manifest_path, EligibleCorpusManifest)
    except ValueError:
        return [
            Diagnostic.from_code(
                "SCHEMA_INVALID_VALUE",
                "the eligible-corpus manifest fails model validation",
                item_ref=run_id,
            )
        ]

    diagnostics: list[Diagnostic] = []
    corpus_path = out_dir / manifest.jsonl_relative_path
    diagnostics.extend(
        verify_file_hash(corpus_path, manifest.jsonl_sha256, item_ref=manifest.jsonl_relative_path)
    )
    if manifest.tokenizer_version != TOKENIZER_VERSION:
        diagnostics.append(
            Diagnostic.from_code(
                "SCHEMA_VERSION_UNSUPPORTED",
                f"manifest tokenizer version {manifest.tokenizer_version!r} does not match "
                f"the current tokenizer {TOKENIZER_VERSION!r}",
                item_ref=run_id,
            )
        )
    if diagnostics:
        return diagnostics

    utterances = list(read_jsonl_models(corpus_path, NormalizedUtterance))
    if len(utterances) != manifest.utterance_count:
        diagnostics.append(
            Diagnostic.from_code(
                "CARDINALITY_MISMATCH",
                f"manifest claims {manifest.utterance_count} utterances but the corpus has "
                f"{len(utterances)}",
                item_ref=run_id,
            )
        )
    recounted = sum(count_words(u.text) for u in utterances)
    if recounted != manifest.english_word_count:
        diagnostics.append(
            Diagnostic.from_code(
                "ARITHMETIC_INVARIANT_VIOLATION",
                f"manifest claims {manifest.english_word_count} English words but an "
                f"independent recount finds {recounted}",
                item_ref=run_id,
            )
        )
    seen: set[str] = set()
    for utterance in utterances:
        if utterance.utterance_id in seen:
            diagnostics.append(
                Diagnostic.from_code(
                    "CARDINALITY_MISMATCH",
                    "duplicate utterance ID in the eligible corpus",
                    item_ref=utterance.utterance_id,
                )
            )
        seen.add(utterance.utterance_id)
    return diagnostics


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Deterministic stage-3 corpus verifier")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--runs-root", type=Path, default=None, help="test override")
    arguments = parser.parse_args(argv)
    diagnostics = verify_corpus(arguments.run_id, runs_root=arguments.runs_root)
    for diagnostic in diagnostics:
        stream = sys.stderr if diagnostic.severity is Severity.ERROR else sys.stdout
        stream.write(f"{diagnostic.severity.value}: {diagnostic.code}: {diagnostic.message}\n")
    if any(d.severity is Severity.ERROR for d in diagnostics):
        return 1
    sys.stdout.write("corpus verification passed\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

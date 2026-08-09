"""CLI: deterministic step-c corpus verifier.

Run: ``uv run python -m glite_english_audit.verification.verify_corpus
--run-id <run-id>`` (tests pass ``--runs-root``).

Step c has no pooled corpus file: the corpus is the set of session files the
agents wrote and ``pipeline.authorship --apply`` verified. So this checks the
index that names them — every listed file still hashes to what apply recorded,
every count and word total recounts, no session file sits in the directory
unlisted, and no utterance ID appears twice across the set.

The unlisted-file check is the one that matters most. A file the index does not
name is a file nothing counted, so its words are missing from the denominator
while its sentences are still on disk for a later step to read.

Prints diagnostics; exits non-zero when any error-level diagnostic fires.
"""

import argparse
import sys
from pathlib import Path

from glite_english_audit.artifacts.enums import StepId
from glite_english_audit.artifacts.io import read_model
from glite_english_audit.diagnostics.codes import Diagnostic, Severity
from glite_english_audit.normalization.tokenizer import TOKENIZER_VERSION, count_words
from glite_english_audit.paths import step_dir
from glite_english_audit.pipeline.authorship import (
    INDEX_NAME,
    AuthoredCorpusIndex,
    corpus_digest,
)
from glite_english_audit.sessions import read_session, session_files
from glite_english_audit.verification.deterministic import verify_file_hash


def verify_corpus(run_id: str, *, runs_root: Path | None = None) -> list[Diagnostic]:
    """Deterministic checks for one run's step-c output."""
    out_dir = step_dir(run_id, StepId.C_AUTHORED, root=runs_root)
    index_path = out_dir / INDEX_NAME
    if not index_path.is_file():
        return [
            Diagnostic.from_code(
                "LINEAGE_MISSING_INPUT",
                "the step-c corpus index does not exist for this run",
                item_ref=run_id,
            )
        ]
    try:
        index = read_model(index_path, AuthoredCorpusIndex)
    except ValueError:
        return [
            Diagnostic.from_code(
                "SCHEMA_INVALID_VALUE",
                "the step-c corpus index fails model validation",
                item_ref=run_id,
            )
        ]

    diagnostics: list[Diagnostic] = []
    if index.tokenizer_version != TOKENIZER_VERSION:
        diagnostics.append(
            Diagnostic.from_code(
                "SCHEMA_VERSION_UNSUPPORTED",
                f"index tokenizer version {index.tokenizer_version!r} does not match "
                f"the current tokenizer {TOKENIZER_VERSION!r}",
                item_ref=run_id,
            )
        )
    listed = {entry.file_name for entry in index.sessions}
    for path in session_files(out_dir):
        if path.name not in listed:
            diagnostics.append(
                Diagnostic.from_code(
                    "CARDINALITY_MISMATCH",
                    "a step-c session file is not listed in the corpus index",
                    item_ref=path.name,
                )
            )
    for entry in index.sessions:
        diagnostics.extend(
            verify_file_hash(out_dir / entry.file_name, entry.sha256, item_ref=entry.file_name)
        )
    if corpus_digest(index.sessions) != index.corpus_sha256:
        diagnostics.append(
            Diagnostic.from_code(
                "LINEAGE_HASH_MISMATCH",
                "the corpus digest does not match the session hashes it summarizes",
                item_ref=run_id,
            )
        )
    # Recounting a file whose bytes already failed reports the same defect
    # twice and, worse, reports it as an arithmetic error.
    if diagnostics:
        return diagnostics

    seen: set[str] = set()
    utterances = 0
    words = 0
    for entry in index.sessions:
        session = read_session(out_dir / entry.file_name)
        utterances += len(session)
        recounted = sum(count_words(utterance.text) for utterance in session)
        words += recounted
        if len(session) != entry.utterance_count:
            diagnostics.append(
                Diagnostic.from_code(
                    "CARDINALITY_MISMATCH",
                    f"the index claims {entry.utterance_count} utterances for this session "
                    f"but the file holds {len(session)}",
                    item_ref=entry.file_name,
                )
            )
        if recounted != entry.word_count:
            diagnostics.append(
                Diagnostic.from_code(
                    "ARITHMETIC_INVARIANT_VIOLATION",
                    f"the index claims {entry.word_count} English words for this session "
                    f"but an independent recount finds {recounted}",
                    item_ref=entry.file_name,
                )
            )
        for utterance in session:
            if utterance.utterance_id in seen:
                diagnostics.append(
                    Diagnostic.from_code(
                        "CARDINALITY_MISMATCH",
                        "duplicate utterance ID in the step-c corpus",
                        item_ref=utterance.utterance_id,
                    )
                )
            seen.add(utterance.utterance_id)

    if len(index.sessions) != index.session_count:
        diagnostics.append(
            Diagnostic.from_code(
                "CARDINALITY_MISMATCH",
                f"the index claims {index.session_count} sessions but lists {len(index.sessions)}",
                item_ref=run_id,
            )
        )
    if utterances != index.utterance_count:
        diagnostics.append(
            Diagnostic.from_code(
                "CARDINALITY_MISMATCH",
                f"the index claims {index.utterance_count} utterances but the corpus holds "
                f"{utterances}",
                item_ref=run_id,
            )
        )
    if words != index.word_count:
        diagnostics.append(
            Diagnostic.from_code(
                "ARITHMETIC_INVARIANT_VIOLATION",
                f"the index claims {index.word_count} English words but an independent "
                f"recount finds {words}",
                item_ref=run_id,
            )
        )
    return diagnostics


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Deterministic step-c corpus verifier")
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

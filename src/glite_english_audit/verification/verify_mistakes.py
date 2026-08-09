"""Stage 5's deterministic verifier: spans, occurrence IDs, double counting.

Run: ``uv run python -m glite_english_audit.verification.verify_mistakes
--run-id <run-id>``

Specification 5.6 requires stage-5 records to be occurrence-based and atomic —
one record per verified occurrence, each with exactly one evidence span and one
occurrence ID, "so verifiers can detect double counting". No such verifier
existed. The only consumer of these records computed ``len(mistakes)``, so the
verified-mistake total, which the user is shown and which Glite receives, was
whatever the model happened to emit.

That total is not a soft number. It is the numerator of the learner's error
rate and the figure every withheld class must add up to, and the risk is real
rather than theoretical: on the measured run the model turned 62 findings into
75 records by splitting blocks that named two errors. A split that names the
same error twice inflates the rate, and nothing downstream could tell.

The checks, all deterministic:

- every occurrence ID appears once;
- every mistake names an utterance the corpus contains;
- every evidence span lies inside that utterance's text, and the text it spans
  is the ``original_text`` the record claims, character for character;
- no two mistakes for one utterance cover overlapping spans.

The last is the double-count check. Two records may sit side by side in one
sentence — a missing article and a wrong preposition are two mistakes — but
they cannot occupy the same characters. Overlap means one error was counted
twice, which is the failure this stage's atomicity rule exists to prevent.
"""

import argparse
import itertools
import json
import sys
from pathlib import Path

from glite_english_audit.artifacts.enums import StepId
from glite_english_audit.artifacts.io import read_jsonl_models
from glite_english_audit.artifacts.models import NormalizedUtterance, PrivateMistake
from glite_english_audit.diagnostics.codes import Diagnostic
from glite_english_audit.paths import step_dir

CORPUS_NAME = "corpus.jsonl"
MISTAKES_NAME = "mistakes.jsonl"


def _overlaps(first: PrivateMistake, second: PrivateMistake) -> bool:
    return (
        first.evidence_span.start < second.evidence_span.end
        and second.evidence_span.start < first.evidence_span.end
    )


def verify_mistakes(mistakes: list[PrivateMistake], corpus: dict[str, str]) -> list[Diagnostic]:
    """Check one run's stage-5 records against the corpus they cite."""
    diagnostics: list[Diagnostic] = []

    seen_occurrences: set[str] = set()
    for mistake in mistakes:
        if mistake.occurrence_id in seen_occurrences:
            diagnostics.append(
                Diagnostic.from_code(
                    "CARDINALITY_MISMATCH",
                    "two records share one occurrence ID, so one occurrence is counted twice",
                    item_ref=mistake.occurrence_id,
                )
            )
        seen_occurrences.add(mistake.occurrence_id)

        text = corpus.get(mistake.utterance_id)
        if text is None:
            diagnostics.append(
                Diagnostic.from_code(
                    "LINEAGE_MISSING_INPUT",
                    "the record cites an utterance the eligible corpus does not contain",
                    item_ref=mistake.mistake_id,
                )
            )
            continue

        span = mistake.evidence_span
        if span.end > len(text):
            diagnostics.append(
                Diagnostic.from_code(
                    "SCHEMA_INVALID_VALUE",
                    "the evidence span runs past the end of its utterance",
                    item_ref=mistake.mistake_id,
                )
            )
            continue
        if text[span.start : span.end] != mistake.original_text:
            # The span is what makes the record checkable. If it does not hold
            # the quoted text, the quote could be a paraphrase or an invention,
            # and nothing later reads the utterance again to find out.
            diagnostics.append(
                Diagnostic.from_code(
                    "SCHEMA_INVALID_VALUE",
                    "the evidence span does not hold the text the record quotes",
                    item_ref=mistake.mistake_id,
                )
            )

    by_utterance: dict[str, list[PrivateMistake]] = {}
    for mistake in mistakes:
        by_utterance.setdefault(mistake.utterance_id, []).append(mistake)
    for group in by_utterance.values():
        ordered = sorted(group, key=lambda m: (m.evidence_span.start, m.evidence_span.end))
        for earlier, later in itertools.pairwise(ordered):
            if _overlaps(earlier, later):
                diagnostics.append(
                    Diagnostic.from_code(
                        "CARDINALITY_MISMATCH",
                        "two records cover overlapping text, so one mistake is counted twice",
                        item_ref=later.mistake_id,
                    )
                )
    return diagnostics


def verify_run(run_id: str, *, runs_root: Path | None = None) -> list[Diagnostic]:
    """Load one run's stage-3 corpus and stage-5 records, then check them."""
    corpus_path = step_dir(run_id, StepId.C_AUTHORED, root=runs_root) / CORPUS_NAME
    corpus = {
        utterance.utterance_id: utterance.text
        for utterance in read_jsonl_models(corpus_path, NormalizedUtterance)
    }
    mistakes_path = step_dir(run_id, StepId.D_MISTAKES, root=runs_root) / MISTAKES_NAME
    mistakes = list(read_jsonl_models(mistakes_path, PrivateMistake))
    return verify_mistakes(mistakes, corpus)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Exits non-zero when anything failed."""
    parser = argparse.ArgumentParser(
        description="Stage 5: verify mistake records deterministically"
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--runs-root", type=Path, default=None, help="test override")
    arguments = parser.parse_args(argv)
    diagnostics = verify_run(arguments.run_id, runs_root=arguments.runs_root)
    sys.stdout.write(
        json.dumps(
            {
                "passed": not diagnostics,
                "diagnostics": [
                    {"code": d.code, "item_ref": d.item_ref, "message": d.message}
                    for d in diagnostics
                ],
            },
            indent=2,
        )
        + "\n"
    )
    return 1 if diagnostics else 0


if __name__ == "__main__":
    raise SystemExit(main())

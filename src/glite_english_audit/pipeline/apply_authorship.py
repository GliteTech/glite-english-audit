"""CLI: stage 3 — verify the model's authorship decisions and build the corpus.

Run: ``uv run python -m glite_english_audit.pipeline.apply_authorship
--run-id <run-id>`` (tests pass ``--runs-root``).

Specification 5.6 splits stage 3 in two. The ``filter-authored-english`` skill
decides which spans of each candidate the learner wrote; this module decides
nothing and counts everything. It re-derives each candidate's text from the
stage-2 records with the same pre-filter the batch driver used, then checks
every decision against it:

- the utterance ID exists in the candidate set and appears in one decision only;
- every retained span occurs in that candidate's text character for character;
- spans do not overlap and follow their order in the text.

The substring check is what keeps paraphrase and invention out of the word
denominator: a span the model repaired, translated, or invented cannot be
located, so it never becomes a counted word. A decision failing any check is
quarantined with a diagnostic code and contributes nothing; it is never
partially accepted and never silently dropped.

Surviving spans are joined with a single newline, classified for language,
deduplicated across sources, and counted with the versioned tokenizer. The
stage-3 corpus and its ``EligibleCorpusManifest`` come out in the shape
``verification.verify_corpus`` already checks. Prints aggregate numbers only,
and exits non-zero when any decision was quarantined, so the orchestrator
repairs the batch instead of publishing a denominator built from part of it.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from glite_english_audit import CLIENT_VERSION
from glite_english_audit.artifacts.enums import StageId, StageStatus
from glite_english_audit.artifacts.envelope import ArtifactEnvelope, utc_now
from glite_english_audit.artifacts.hashing import new_artifact_id, sha256_hex
from glite_english_audit.artifacts.io import ensure_private_dir, write_jsonl_models, write_model
from glite_english_audit.artifacts.models import EligibleCorpusManifest, NormalizedUtterance
from glite_english_audit.diagnostics.codes import Diagnostic, Severity
from glite_english_audit.normalization.dedup import dedupe
from glite_english_audit.normalization.language import classify_english
from glite_english_audit.normalization.tokenizer import TOKENIZER_VERSION, count_words
from glite_english_audit.paths import stage_dir
from glite_english_audit.pipeline.authorship_batches import (
    build_candidates,
    decisions_dir,
    read_candidate_utterances,
)
from glite_english_audit.pipeline.record_stage import advance_to

CORPUS_NAME = "corpus.jsonl"
MANIFEST_NAME = "eligible-corpus-manifest.json"
DECISIONS_GLOB = "decisions-*.jsonl"
PRODUCER_NAME = "apply_authorship"
REPAIR_NAME = "needs-repair.json"

DecisionKind = Literal["retain", "partial", "exclude"]

# The closed reason list of skills/filter-authored-english/SKILL.md. These are
# decision payload values, not diagnostics: they say what the model excluded,
# never that something went wrong.
RETENTION_REASON_CODES: frozenset[str] = frozenset(
    {
        "AUTHORSHIP_AGENT_MACHINERY",
        "AUTHORSHIP_TOOL_OUTPUT",
        "AUTHORSHIP_CODE",
        "AUTHORSHIP_PASTED_MATERIAL",
        "AUTHORSHIP_OTHER_SPEAKER",
        "AUTHORSHIP_REFERENCE_ONLY",
        "AUTHORSHIP_UNCLEAR",
    }
)

_DECISION_KEYS = frozenset({"utterance_id", "decision", "retained_spans", "reason"})


class AuthorshipDecision(BaseModel):
    """One model decision about one candidate utterance."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    utterance_id: str
    decision: DecisionKind
    retained_spans: list[str] = Field(default_factory=list)
    reason: str | None = None


class AuthorshipApplication(BaseModel):
    """What the model judged and what the tokenizer counted, in numbers only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest: EligibleCorpusManifest
    candidates_in: int = Field(ge=0)
    decisions_read: int = Field(ge=0)
    retained: int = Field(ge=0)
    partial: int = Field(ge=0)
    excluded: int = Field(ge=0)
    quarantined_decisions: int = Field(ge=0)
    missing_decisions: int = Field(ge=0)
    quarantined_language: int = Field(ge=0)
    words_before: int = Field(ge=0)
    words_after: int = Field(ge=0)
    diagnostics: list[Diagnostic] = Field(default_factory=list)


def _parse_line(line: str, *, source: str, number: int) -> AuthorshipDecision | Diagnostic:
    """Validate one decision line's JSON shape before its content is used."""
    reference = f"{source}:{number}"
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return Diagnostic.from_code(
            "SCHEMA_INVALID_JSON", "a decision line is not valid JSON", item_ref=reference
        )
    if not isinstance(payload, dict):
        return Diagnostic.from_code(
            "SCHEMA_INVALID_VALUE", "a decision line is not a JSON object", item_ref=reference
        )
    keys = set(payload)
    missing = sorted(_DECISION_KEYS - keys)
    if missing:
        return Diagnostic.from_code(
            "SCHEMA_MISSING_FIELD",
            f"a decision line is missing {', '.join(missing)}",
            item_ref=reference,
        )
    unexpected = sorted(keys - _DECISION_KEYS)
    if unexpected:
        return Diagnostic.from_code(
            "SCHEMA_UNEXPECTED_FIELD",
            f"a decision line carries undeclared field {', '.join(unexpected)}",
            item_ref=reference,
        )
    try:
        return AuthorshipDecision.model_validate(payload)
    except ValidationError:
        return Diagnostic.from_code(
            "SCHEMA_INVALID_VALUE", "a decision line fails model validation", item_ref=reference
        )


def read_decisions(paths: list[Path]) -> tuple[list[AuthorshipDecision], list[Diagnostic]]:
    """Read decision JSONL files in the given order, reporting bad lines."""
    decisions: list[AuthorshipDecision] = []
    diagnostics: list[Diagnostic] = []
    for path in paths:
        for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not raw.strip():
                continue
            parsed = _parse_line(raw, source=path.name, number=number)
            if isinstance(parsed, Diagnostic):
                diagnostics.append(parsed)
            else:
                decisions.append(parsed)
    return decisions, diagnostics


def _shape_diagnostic(decision: AuthorshipDecision, text: str) -> Diagnostic | None:
    """Check a decision against its own declared kind, before span matching."""
    spans = decision.retained_spans
    if any(not span for span in spans):
        return Diagnostic.from_code(
            "SCHEMA_INVALID_VALUE",
            "a retained span is the empty string",
            item_ref=decision.utterance_id,
        )
    if decision.decision == "retain":
        if decision.reason is not None:
            return Diagnostic.from_code(
                "SCHEMA_INVALID_VALUE",
                "a retain decision carries an exclusion reason",
                item_ref=decision.utterance_id,
            )
        if spans != [text]:
            return Diagnostic.from_code(
                "SCHEMA_INVALID_VALUE",
                "a retain decision does not carry the whole candidate text as one span",
                item_ref=decision.utterance_id,
            )
        return None
    if decision.reason not in RETENTION_REASON_CODES:
        return Diagnostic.from_code(
            "SCHEMA_INVALID_VALUE",
            "a decision carries a reason outside the closed list",
            item_ref=decision.utterance_id,
        )
    if decision.decision == "exclude" and spans:
        return Diagnostic.from_code(
            "SCHEMA_INVALID_VALUE",
            "an exclude decision carries retained spans",
            item_ref=decision.utterance_id,
        )
    if decision.decision == "partial" and not spans:
        return Diagnostic.from_code(
            "SCHEMA_INVALID_VALUE",
            "a partial decision carries no retained span",
            item_ref=decision.utterance_id,
        )
    return None


def _span_diagnostic(decision: AuthorshipDecision, text: str) -> Diagnostic | None:
    """Locate every span in ``text``, in order and without overlap."""
    cursor = 0
    for span in decision.retained_spans:
        position = text.find(span, cursor)
        if position >= 0:
            cursor = position + len(span)
            continue
        if span in text:
            return Diagnostic.from_code(
                "AUTHORSHIP_SPAN_ORDER_INVALID",
                "a retained span overlaps an earlier span or breaks their original order",
                item_ref=decision.utterance_id,
            )
        return Diagnostic.from_code(
            "AUTHORSHIP_SPAN_NOT_VERBATIM",
            "a retained span is not an exact substring of the candidate text",
            item_ref=decision.utterance_id,
        )
    return None


def verify_decision(decision: AuthorshipDecision, text: str) -> Diagnostic | None:
    """Return the diagnostic that quarantines ``decision``, or ``None``."""
    return _shape_diagnostic(decision, text) or _span_diagnostic(decision, text)


def eligible_text(decision: AuthorshipDecision) -> str:
    """The utterance text built from the retained spans, in order."""
    return "\n".join(decision.retained_spans)


def _decision_paths(run_id: str, runs_root: Path | None, decisions_root: Path | None) -> list[Path]:
    root = decisions_root
    if root is None:
        root = decisions_dir(run_id, runs_root=runs_root)
    paths = sorted(root.glob(DECISIONS_GLOB)) if root.is_dir() else []
    if not paths:
        msg = f"no {DECISIONS_GLOB} file found in {root}"
        raise FileNotFoundError(msg)
    return paths


def apply_authorship(
    run_id: str, *, runs_root: Path | None = None, decisions_root: Path | None = None
) -> AuthorshipApplication:
    """Verify this run's decisions and write the stage-3 corpus and manifest."""
    stage_two = read_candidate_utterances(run_id, runs_root=runs_root)
    source_by_id = {utterance.utterance_id: utterance for utterance in stage_two}
    candidates = build_candidates(stage_two)
    candidate_by_id = {candidate.utterance_id: candidate for candidate in candidates}

    decisions, diagnostics = read_decisions(_decision_paths(run_id, runs_root, decisions_root))
    counts = {"retain": 0, "partial": 0, "exclude": 0}
    quarantined_decisions = len(diagnostics)
    quarantined_language = 0
    eligible: list[NormalizedUtterance] = []
    seen: set[str] = set()

    for decision in decisions:
        candidate = candidate_by_id.get(decision.utterance_id)
        if candidate is None:
            quarantined_decisions += 1
            diagnostics.append(
                Diagnostic.from_code(
                    "AUTHORSHIP_UNKNOWN_UTTERANCE",
                    "a decision names an utterance that is not a candidate of this run",
                    item_ref=decision.utterance_id,
                )
            )
            continue
        if decision.utterance_id in seen:
            quarantined_decisions += 1
            diagnostics.append(
                Diagnostic.from_code(
                    "AUTHORSHIP_DUPLICATE_DECISION",
                    "more than one decision covers this candidate",
                    item_ref=decision.utterance_id,
                )
            )
            continue
        seen.add(decision.utterance_id)

        diagnostic = verify_decision(decision, candidate.text)
        if diagnostic is not None:
            quarantined_decisions += 1
            diagnostics.append(diagnostic)
            continue

        counts[decision.decision] += 1
        retained = eligible_text(decision).strip()
        if not retained:
            continue
        classified = classify_english(retained)
        if classified.quarantined or classified.english_text is None:
            quarantined_language += 1
            continue
        update = {"text": classified.english_text}
        eligible.append(source_by_id[decision.utterance_id].model_copy(update=update))

    missing = sorted(set(candidate_by_id) - seen)
    diagnostics.extend(
        Diagnostic.from_code(
            "CARDINALITY_MISMATCH",
            "a candidate utterance has no decision line",
            item_ref=utterance_id,
        )
        for utterance_id in missing
    )

    outcome = dedupe(eligible)
    out_dir = ensure_private_dir(stage_dir(run_id, StageId.ELIGIBLE_ENGLISH, root=runs_root))
    corpus_path = out_dir / CORPUS_NAME
    written = write_jsonl_models(corpus_path, outcome.canonical)
    words_after = sum(count_words(u.text) for u in outcome.canonical)

    manifest = EligibleCorpusManifest(
        envelope=ArtifactEnvelope(
            schema_name="eligible_corpus",
            schema_version=1,
            artifact_id=new_artifact_id(),
            run_id=run_id,
            stage_id=StageId.ELIGIBLE_ENGLISH,
            producer_name=PRODUCER_NAME,
            producer_version=CLIENT_VERSION,
            created_at=utc_now(),
        ),
        tokenizer_version=TOKENIZER_VERSION,
        utterance_count=written,
        english_word_count=words_after,
        quarantined_utterance_count=quarantined_decisions + quarantined_language + len(missing),
        deduplicated_utterance_count=len(outcome.excluded),
        jsonl_relative_path=CORPUS_NAME,
        jsonl_sha256=sha256_hex(corpus_path.read_bytes()),
    )
    write_model(out_dir / MANIFEST_NAME, manifest)
    _write_repair_list(run_id, diagnostics, runs_root=runs_root, decisions_root=decisions_root)
    # The corpus is durable, so the manifest may point at it. Stage 3 is
    # deterministic once the model's decisions are in hand: the span verifier
    # above is the check, and a decision that fails it is quarantined rather
    # than corrected, so there is no second opinion left to wait for.
    advance_to(
        run_id,
        StageId.ELIGIBLE_ENGLISH,
        StageStatus.PROMOTED,
        artifact_id=manifest.envelope.artifact_id,
        artifact_hash=manifest.jsonl_sha256,
        producer_version=CLIENT_VERSION,
        runs_root=runs_root,
    )

    return AuthorshipApplication(
        manifest=manifest,
        candidates_in=len(candidates),
        decisions_read=len(decisions),
        retained=counts["retain"],
        partial=counts["partial"],
        excluded=counts["exclude"],
        quarantined_decisions=quarantined_decisions,
        missing_decisions=len(missing),
        quarantined_language=quarantined_language,
        words_before=sum(count_words(candidate.text) for candidate in candidates),
        words_after=words_after,
        diagnostics=diagnostics,
    )


def _diagnostic_counts(diagnostics: list[Diagnostic]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for diagnostic in diagnostics:
        counts[diagnostic.code] = counts.get(diagnostic.code, 0) + 1
    return dict(sorted(counts.items()))


def repair_list_path(
    run_id: str, *, runs_root: Path | None = None, decisions_root: Path | None = None
) -> Path:
    """Where the list of utterances still needing a judgment is written."""
    base = (
        decisions_root if decisions_root is not None else decisions_dir(run_id, runs_root=runs_root)
    )
    return base / REPAIR_NAME


def _write_repair_list(
    run_id: str,
    diagnostics: list[Diagnostic],
    *,
    runs_root: Path | None = None,
    decisions_root: Path | None = None,
) -> Path:
    """Record which utterances need re-judging, and why.

    A quarantined decision loses the whole utterance, including the spans that
    were fine — on real data one model reply returned nested spans covering the
    same tail three times, and rejecting it discarded two good spans with the
    bad ones. Specification 6.4 allows bounded repair, so the failures are
    listed here by utterance and diagnostic code, letting a repair pass re-ask
    for exactly those rather than redoing a batch or accepting the loss.

    The list holds identifiers and codes only, never text.
    """
    items = [
        {"utterance_id": diagnostic.item_ref, "code": diagnostic.code}
        for diagnostic in diagnostics
        if diagnostic.item_ref is not None
    ]
    target = repair_list_path(run_id, runs_root=runs_root, decisions_root=decisions_root)
    ensure_private_dir(target.parent)
    target.write_text(
        json.dumps({"needs_repair": items}, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return target


def main(argv: list[str] | None = None) -> int:
    """CLI entry point printing aggregate counts as JSON."""
    parser = argparse.ArgumentParser(description="Stage 3: apply verified authorship decisions")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--decisions-dir", type=Path, default=None)
    parser.add_argument("--runs-root", type=Path, default=None, help="test override")
    arguments = parser.parse_args(argv)
    result = apply_authorship(
        arguments.run_id, runs_root=arguments.runs_root, decisions_root=arguments.decisions_dir
    )
    sys.stdout.write(
        json.dumps(
            {
                "candidates_in": result.candidates_in,
                "decisions_read": result.decisions_read,
                "retained": result.retained,
                "partial": result.partial,
                "excluded": result.excluded,
                "quarantined_decisions": result.quarantined_decisions,
                "needs_repair": str(
                    repair_list_path(
                        arguments.run_id,
                        runs_root=arguments.runs_root,
                        decisions_root=arguments.decisions_dir,
                    )
                ),
                "missing_decisions": result.missing_decisions,
                "quarantined_language": result.quarantined_language,
                "words_before": result.words_before,
                "words_after": result.words_after,
                "eligible_utterances": result.manifest.utterance_count,
                "deduplicated_utterances": result.manifest.deduplicated_utterance_count,
                "tokenizer_version": result.manifest.tokenizer_version,
                "diagnostic_codes": _diagnostic_counts(result.diagnostics),
            },
            indent=2,
        )
        + "\n"
    )
    return 1 if any(d.severity is Severity.ERROR for d in result.diagnostics) else 0


if __name__ == "__main__":
    raise SystemExit(main())

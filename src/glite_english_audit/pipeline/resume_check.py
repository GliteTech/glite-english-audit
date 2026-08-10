"""CLI: what the launcher needs to answer "is there an audit to continue?".

Run: ``uv run python -m glite_english_audit.pipeline.resume_check``

Step 2 of the run skill has always described this decision exactly -- list the
unfinished runs, compare each one's recorded fingerprint against the current
one, and offer the compatible ones. It just had no command. ``list_unfinished``
and ``describe_resume`` were written, tested, and called by nothing, so the
agent facing that step had to build the answer itself: a real session opened
with two ``python -c`` snippets, a ``sed`` through ``save_choice.py``, an ``ls``
of the adapters package, and sixty lines of a test file, before it could say
"no unfinished audit". The user watched all of it. That is the defect this
repository keeps finding, and the fix is always the same shape -- a fact the
agent must fetch for itself is a fact it fetches differently every time.

Three things happen here, in this order, because each depends on the last.

**Retention runs first.** Opening the launcher is the moment the product is
demonstrably alive, and the thirty-day rule is a promise about text on disk
rather than about a run anyone remembers. Sweeping before listing also means
nothing expired is ever offered for resume.

**Debris is removed.** A start that failed before writing a manifest leaves a
directory holding nothing. It cannot be resumed, cannot be expired -- there is
no timestamped private file to date it from -- and it is not a run. Left there
it accumulates, and the launcher ends up explaining "an empty directory from an
aborted start, with no manifest" to somebody who asked whether they had an
unfinished audit. Only genuinely empty directories go: anything holding a file
is somebody's data until retention says otherwise.

**Then the decision.** For each surviving run the recorded fingerprint is
compared against the one this checkout would produce for that run's own
adapters, and the deterministic policy returns continue, recompute, restart or
refuse with the reason already worded.

Output is aggregate and structural: run ids, timestamps, statuses, decisions.
No path, no label, no source text.
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from glite_english_audit.artifacts.manifest import (
    MANIFEST_SCHEMA_VERSION,
    CompatibilityFingerprint,
    RunManifest,
)
from glite_english_audit.consent import CONSENT_POLICY_VERSION
from glite_english_audit.normalization.tokenizer import TOKENIZER_VERSION
from glite_english_audit.paths import repo_root, runs_root
from glite_english_audit.runtime_session import observed_model_ids
from glite_english_audit.state.run_store import (
    ResumeDecision,
    RunStoreError,
    describe_resume,
    expire_stale_runs,
    list_unfinished,
    load_manifest,
)
from glite_english_audit.verification.skills import skill_versions

# The two decisions that mean work already done can still be used. `restart`
# and `expired` both end with "start a new run", so a launcher that offered them
# would be offering something the policy has already refused.
#
# Built from the enum rather than written as strings. The first version of this
# filtered on `!= "refuse"` -- a value ResumeDecision has never had -- so it
# excluded nothing, and a run whose own detail read "Checkpointed artifacts
# cannot be reused. Start a new run." was counted as continuable. A string
# compared against an enum fails silently in exactly this direction: it always
# looks like the permissive answer.
_CONTINUABLE = frozenset(
    {ResumeDecision.CONTINUE.value, ResumeDecision.INVALIDATE_DOWNSTREAM.value}
)


def live_adapter_versions(recorded: dict[str, str]) -> dict[str, str]:
    """Today's version of each adapter the run selected.

    Keyed by what the run recorded rather than by everything installed: resume
    asks whether *this* run's sources changed under it, and an adapter it never
    touched changing version is not that run's problem. An adapter that has
    since been removed keeps its recorded version here, so the comparison
    reports no change and the removal surfaces where it actually bites --
    collection -- rather than as a fingerprint mismatch nobody can act on.
    """
    from glite_english_audit.adapters import register_all
    from glite_english_audit.discovery.registry import adapter_ids, create_adapter

    register_all()
    installed = set(adapter_ids())
    live: dict[str, str] = {}
    for adapter_id, recorded_version in recorded.items():
        if adapter_id in installed:
            live[adapter_id] = create_adapter(adapter_id).adapter_version
        else:
            live[adapter_id] = recorded_version
    return live


def current_fingerprint(manifest: RunManifest) -> CompatibilityFingerprint:
    """What this checkout would record for the run in ``manifest`` today."""
    return CompatibilityFingerprint(
        adapter_versions=live_adapter_versions(manifest.fingerprint.adapter_versions),
        artifact_schema_version=MANIFEST_SCHEMA_VERSION,
        tokenizer_version=TOKENIZER_VERSION,
        skill_versions=skill_versions(repo_root()),
        prompt_versions={},
        model_ids=observed_model_ids(),
        consent_policy_version=CONSENT_POLICY_VERSION,
    )


def remove_empty_run_dirs(root: Path) -> list[str]:
    """Delete run directories that hold no file at all, and name them.

    A failed start leaves one. It is not resumable, not expirable, and not a
    run; the only thing it does is get reported to a user as machinery. A
    directory holding any file is left alone -- that is retention's decision,
    not this function's.
    """
    if not root.is_dir():
        return []
    removed: list[str] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.is_symlink():
            continue
        if any(path.is_file() for path in child.rglob("*")):
            continue
        for directory in sorted(child.rglob("*"), reverse=True):
            if directory.is_dir() and not directory.is_symlink():
                directory.rmdir()
        child.rmdir()
        removed.append(child.name)
    return removed


def build_report(*, root: Path | None = None, now: datetime | None = None) -> dict[str, object]:
    """Sweep, tidy, then assess every unfinished run."""
    base = root if root is not None else runs_root()
    moment = now if now is not None else datetime.now(UTC)

    expired = expire_stale_runs(base, now=moment)
    removed = remove_empty_run_dirs(base)

    runs: list[dict[str, object]] = []
    for summary in list_unfinished(base, now=moment):
        try:
            manifest = load_manifest(summary.run_id, root=base)
        except (RunStoreError, OSError, ValueError):
            # list_unfinished already skips what it cannot read; a failure here
            # is a file changing under us mid-scan, which is not resumable.
            continue
        assessment = describe_resume(manifest, current_fingerprint(manifest), now=moment)
        runs.append(
            {
                "run_id": summary.run_id,
                "started_at": summary.started_at.isoformat(),
                "status": summary.status.value,
                "last_promoted_step": (
                    summary.last_promoted_step.name.lower()
                    if summary.last_promoted_step is not None
                    else None
                ),
                "checkpoint_age_hours": round(summary.checkpoint_age.total_seconds() / 3600, 1),
                "decision": assessment.decision.value,
                "detail": assessment.detail,
                "earliest_affected_step": (
                    assessment.earliest_affected_step.name.lower()
                    if assessment.earliest_affected_step is not None
                    else None
                ),
                "diagnostic": (
                    assessment.diagnostic.code if assessment.diagnostic is not None else None
                ),
            }
        )

    offerable = [run for run in runs if run["decision"] in _CONTINUABLE]
    return {
        "unfinished": runs,
        "offerable": len(offerable),
        "expired_by_retention": expired,
        "removed_empty": removed,
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Prints run ids, times and decisions; never source text."""
    parser = argparse.ArgumentParser(
        description="List unfinished runs and the deterministic resume decision for each"
    )
    parser.add_argument(
        "--runs-root",
        type=Path,
        default=None,
        help="Run store root; defaults to the location paths.runs_root() reports",
    )
    args = parser.parse_args(argv)
    report = build_report(root=args.runs_root)
    sys.stdout.write(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())

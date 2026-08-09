"""CLI: create a run and record the user's source and period selection.

Run: ``uv run python -m glite_english_audit.pipeline.start_run --run-dir ...``

Reads the stage-0 private inventory, resolves which instances the user chose,
freezes the record-level source cutoff, and writes the run manifest. The
cutoff makes later resumption deterministic: records created after it belong
to the next audit (specification, 13.5).
"""

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from glite_english_audit import CLIENT_VERSION
from glite_english_audit.artifacts.enums import (
    Accessibility,
    AgentRuntime,
    RunStatus,
    Stability,
    StageId,
)
from glite_english_audit.artifacts.envelope import utc_now
from glite_english_audit.artifacts.hashing import new_run_id
from glite_english_audit.artifacts.io import ensure_private_dir, read_model, write_model
from glite_english_audit.artifacts.manifest import (
    MANIFEST_SCHEMA_VERSION,
    CompatibilityFingerprint,
    ConsentState,
    PeriodSelection,
    RunManifest,
    SelectionState,
    empty_stage_map,
)
from glite_english_audit.consent import CONSENT_POLICY_VERSION
from glite_english_audit.discovery.inventory import PrivateInventory
from glite_english_audit.discovery.pending_expiry import (
    PENDING_INVENTORY_MAX_AGE_DAYS,
    is_stale,
)
from glite_english_audit.estimation.profile import load_token_usage_profile, resolve_models
from glite_english_audit.normalization.tokenizer import TOKENIZER_VERSION
from glite_english_audit.paths import pending_inventory_dir, run_dir, stage_dir
from glite_english_audit.pipeline.save_choice import load_choice

INVENTORY_NAME = "source-inventory.json"
MANIFEST_NAME = "run-manifest.json"

DEFAULT_PERIOD = "last-30-days"
DEFAULT_PROFILE = "recommended"

PERIOD_PRESETS: dict[str, int | None] = {
    "last-7-days": 7,
    "last-30-days": 30,
    "last-3-months": 91,
    "last-year": 365,
    "everything": None,
}


def resolve_period(preset: str, now: datetime) -> PeriodSelection:
    """Turn a preset name into concrete UTC bounds."""
    if preset not in PERIOD_PRESETS:
        msg = f"unknown period preset: {preset!r}; choose one of {sorted(PERIOD_PRESETS)}"
        raise ValueError(msg)
    days = PERIOD_PRESETS[preset]
    start = datetime(1970, 1, 1, tzinfo=UTC) if days is None else now - timedelta(days=days)
    return PeriodSelection(preset=preset, start=start, end=now)


def _matches_source(record: object, name: str) -> bool:
    """True when ``name`` names this record's application.

    Accepts either the stable public ID (``claude_code``) or the human name the
    user actually saw, which is the opaque label with its number removed
    (``Claude Code 4`` -> ``Claude Code``).
    """
    wanted = name.strip().casefold()
    adapter_id = getattr(record, "adapter_id", "")
    label = getattr(record, "opaque_label", "")
    human = label.rsplit(" ", 1)[0] if label else ""
    return wanted in {adapter_id.casefold(), human.casefold(), label.casefold()}


def resolve_selection(
    inventory: PrivateInventory,
    *,
    include_sources: list[str] | None = None,
    exclude_sources: list[str] | None = None,
    exclude_labels: list[str] | None = None,
) -> list[str]:
    """Turn the user's spoken choice into instance keys.

    The agent only ever sees opaque labels and application names — instance
    keys stay in the private inventory — so the choice arrives in those terms
    and is resolved here, locally (specification, 2.4).

    Start from the default selection, add whole applications the user asked
    for, then remove whatever they excluded.
    """
    selected = set(default_selection(inventory))
    for name in include_sources or []:
        selected.update(
            record.instance_key
            for record in inventory.records
            if _matches_source(record, name)
            and record.accessibility is Accessibility.FOUND
            and record.candidate_messages > 0
        )
    for name in exclude_sources or []:
        selected.difference_update(
            record.instance_key for record in inventory.records if _matches_source(record, name)
        )
    wanted_labels = {label.strip().casefold() for label in exclude_labels or []}
    if wanted_labels:
        selected.difference_update(
            record.instance_key
            for record in inventory.records
            if record.opaque_label.casefold() in wanted_labels
        )
    return sorted(selected)


def default_selection(inventory: PrivateInventory) -> list[str]:
    """Stable, found instances with a supported schema, per specification 2.4.

    Beta, experimental, inaccessible, and unsupported-schema instances are
    never selected automatically.
    """
    return [
        record.instance_key
        for record in inventory.records
        if record.stability is Stability.STABLE
        and record.accessibility is Accessibility.FOUND
        and record.candidate_messages > 0
    ]


def start_run(
    *,
    runtime: AgentRuntime,
    os_environment_value: str,
    preset: str | None,
    instance_keys: list[str] | None,
    processing_profile: str | None,
    runs_root: Path | None,
    inventory_dir: Path,
    include_sources: list[str] | None = None,
    exclude_sources: list[str] | None = None,
    exclude_labels: list[str] | None = None,
    local_scan_consent: bool = False,
    provider_transfer_consent: bool = False,
    now: datetime | None = None,
) -> RunManifest:
    """Create the run directory, manifest, and frozen selection.

    Consent is recorded only when the caller states it was given. Neither flag
    defaults to true: a consent timestamp is evidence that a person was asked
    and agreed, and inventing one for a step that may never have happened is
    worse than inferring it from a previous run, which specification 2.2 already
    forbids. A run may legitimately exist with neither: creating it records the
    selection, and processing is what needs the agreement.
    """
    from glite_english_audit.artifacts.enums import OsEnvironment

    moment = now if now is not None else utc_now()
    inventory = read_model(inventory_dir / INVENTORY_NAME, PrivateInventory)
    if is_stale(inventory, now=moment):
        # Starting from a stale map means snapshotting paths that may no longer
        # be what they were. Rediscovering costs seconds; auditing the wrong
        # sources costs the whole run.
        msg = (
            "this source inventory is older than "
            f"{PENDING_INVENTORY_MAX_AGE_DAYS} days, so it may no longer describe "
            "this machine; run discovery again before starting a run"
        )
        raise ValueError(msg)

    # A choice the user already made during setup is used unless this call
    # overrides it, so an answer given once does not have to be repeated.
    remembered = load_choice(inventory_dir=inventory_dir)
    if remembered is not None:
        if preset is None:
            preset = remembered.period_preset
        if processing_profile is None:
            processing_profile = remembered.processing_profile
        if include_sources is None:
            include_sources = remembered.include_sources
        if exclude_sources is None:
            exclude_sources = remembered.exclude_sources
        if exclude_labels is None:
            exclude_labels = remembered.exclude_labels
    if preset is None:
        preset = DEFAULT_PERIOD
    if processing_profile is None:
        processing_profile = DEFAULT_PROFILE
    selected = (
        instance_keys
        if instance_keys
        else resolve_selection(
            inventory,
            include_sources=include_sources,
            exclude_sources=exclude_sources,
            exclude_labels=exclude_labels,
        )
    )
    if not selected:
        msg = "no eligible source instance was selected; nothing to audit"
        raise ValueError(msg)
    known = {record.instance_key for record in inventory.records}
    unknown = sorted(set(selected) - known)
    if unknown:
        msg = f"selection names instances absent from the inventory: {unknown}"
        raise ValueError(msg)

    adapter_versions = {
        record.adapter_id: record.adapter_version
        for record in inventory.records
        if record.instance_key in set(selected)
    }
    run_id = new_run_id()
    manifest = RunManifest(
        manifest_schema_version=MANIFEST_SCHEMA_VERSION,
        run_id=run_id,
        created_at=moment,
        runtime=runtime,
        os_environment=OsEnvironment(os_environment_value),
        status=RunStatus.AWAITING_PREFLIGHT,
        consent=ConsentState(
            consent_policy_version=CONSENT_POLICY_VERSION,
            local_scan_confirmed_at=moment if local_scan_consent else None,
            provider_transfer_confirmed_at=moment if provider_transfer_consent else None,
        ),
        selection=SelectionState(
            selected_instance_keys=sorted(selected),
            excluded_instance_keys=sorted(known - set(selected)),
            period=resolve_period(preset, moment),
            processing_profile=processing_profile,
            record_cutoff_at=moment,
        ),
        stages=empty_stage_map(),
        fingerprint=CompatibilityFingerprint(
            adapter_versions=adapter_versions,
            artifact_schema_version=MANIFEST_SCHEMA_VERSION,
            tokenizer_version=TOKENIZER_VERSION,
            skill_versions={},
            prompt_versions={},
            # The models this profile resolves to, per specification 10.8. They
            # were empty, which cost two things: the manifest did not record
            # what ran, and resume compares this field to decide whether a
            # model change invalidates the semantic stages — an empty dict is
            # equal to an empty dict forever, so that check could never fire.
            model_ids=resolve_models(
                load_token_usage_profile(),
                runtime=runtime.value.replace("_", "-"),
                processing_profile=processing_profile,
            ),
            consent_policy_version=CONSENT_POLICY_VERSION,
        ),
    )
    base = runs_root / run_id if runs_root is not None else run_dir(run_id)
    ensure_private_dir(base)
    for name in ("stages", "logs", "submission"):
        ensure_private_dir(base / name)
    # Carry the inventory into the run so later stages resolve labels locally.
    inventory_target = stage_dir(run_id, StageId.SOURCE_INVENTORY, root=runs_root)
    ensure_private_dir(inventory_target)
    write_model(inventory_target / INVENTORY_NAME, inventory)
    write_model(base / MANIFEST_NAME, manifest)
    return manifest


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Create an audit run and freeze its selection")
    parser.add_argument(
        "--inventory-dir",
        type=Path,
        default=None,
        help="defaults to the inventory that discovery left pending",
    )
    parser.add_argument("--runtime", default="claude_code", choices=[r.value for r in AgentRuntime])
    parser.add_argument("--os-environment", default="macos")
    parser.add_argument(
        "--period",
        default=None,
        choices=sorted(PERIOD_PRESETS),
        help="defaults to the remembered choice, then to " + DEFAULT_PERIOD,
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="defaults to the remembered choice, then to " + DEFAULT_PROFILE,
    )
    parser.add_argument(
        "--instance-key",
        action="append",
        default=None,
        help="exact instance key; normally the label options below are used instead",
    )
    parser.add_argument(
        "--include-source",
        action="append",
        default=None,
        metavar="APP",
        help="add every found instance of this app, by public ID or the name shown "
        "to the user (repeatable)",
    )
    parser.add_argument(
        "--exclude-source",
        action="append",
        default=None,
        metavar="APP",
        help="drop every instance of this app (repeatable)",
    )
    parser.add_argument(
        "--exclude-label",
        action="append",
        default=None,
        metavar="LABEL",
        help='drop one instance by the label the user saw, such as "Claude Code 4" (repeatable)',
    )
    parser.add_argument(
        "--local-scan-consent",
        action="store_true",
        help="the user confirmed that local scripts may read their source data",
    )
    parser.add_argument(
        "--provider-transfer-consent",
        action="store_true",
        help="the user confirmed that the selected text may go to their AI provider; "
        "pass this only after asking, on this run",
    )
    parser.add_argument("--runs-root", type=Path, default=None, help="test override")
    arguments = parser.parse_args(argv)

    manifest = start_run(
        runtime=AgentRuntime(arguments.runtime),
        os_environment_value=arguments.os_environment,
        preset=arguments.period,
        instance_keys=arguments.instance_key,
        include_sources=arguments.include_source,
        exclude_sources=arguments.exclude_source,
        exclude_labels=arguments.exclude_label,
        local_scan_consent=arguments.local_scan_consent,
        provider_transfer_consent=arguments.provider_transfer_consent,
        processing_profile=arguments.profile,
        runs_root=arguments.runs_root,
        inventory_dir=(
            arguments.inventory_dir
            if arguments.inventory_dir is not None
            else pending_inventory_dir()
        ),
    )
    selection = manifest.selection
    assert selection is not None
    sys.stdout.write(
        json.dumps(
            {
                "run_id": manifest.run_id,
                "client_version": CLIENT_VERSION,
                "selected_instances": len(selection.selected_instance_keys),
                "excluded_instances": len(selection.excluded_instance_keys),
                "period": selection.period.preset,
                "period_start": selection.period.start.isoformat(),
                "period_end": selection.period.end.isoformat(),
                "processing_profile": selection.processing_profile,
                "local_scan_consent": manifest.consent.local_scan_confirmed_at is not None,
                "provider_transfer_consent": (
                    manifest.consent.provider_transfer_confirmed_at is not None
                ),
            },
            indent=2,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

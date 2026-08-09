"""Record a consent moment in the run manifest.

Run: ``uv run python -m glite_english_audit.pipeline.record_consent
--run-id <id> --moment preflight``

Specification 2.2 names four consent moments. Two of them had a field on
:class:`ConsentState` and no code path that ever set it, and the fourth had no
field at all: the review page held the age attestation and the storage
acceptance in memory and the server wrote nothing to disk. So a finished run
could claim in its own manifest that only half its consents existed, and an
auditor reading that manifest would have no way to tell a consent that was
never asked for from one that was given and never written down.

That distinction is the entire point of a consent record. Recording nothing is
not the safe default here — it is the failure.

Three rules the moments do not share:

- Local-scan consent may be remembered across runs until the policy version
  changes. The other three may not.
- Provider-transfer consent is per-run and is never inferred from anything,
  including a previous run of the same audit (specification, 2.2).
- The two submission confirmations are separate values because the page shows
  two separate unchecked boxes. Either can be given without the other, and
  writing one timestamp for both would record a single act the user never
  performed.

Recording is idempotent by moment: a moment already stamped keeps its original
time. A consent is evidence that a person was asked and agreed at a particular
moment, and moving that timestamp later would misdate the evidence.
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from glite_english_audit.artifacts.envelope import utc_now
from glite_english_audit.artifacts.manifest import RunManifest
from glite_english_audit.state.run_store import load_manifest, save_manifest

MOMENTS: tuple[str, ...] = (
    "local-scan",
    "provider-transfer",
    "preflight",
    "adult",
    "storage-terms",
)

_FIELDS: dict[str, str] = {
    "local-scan": "local_scan_confirmed_at",
    "provider-transfer": "provider_transfer_confirmed_at",
    "preflight": "preflight_confirmed_at",
    "adult": "adult_confirmed_at",
    "storage-terms": "storage_terms_confirmed_at",
}


def record_consent(
    run_id: str,
    moment: str,
    *,
    runs_root: Path | None = None,
    now: datetime | None = None,
) -> RunManifest:
    """Stamp one consent moment, if it is not already stamped."""
    field = _FIELDS.get(moment)
    if field is None:
        msg = f"unknown consent moment: {moment!r}; choose one of {list(MOMENTS)}"
        raise ValueError(msg)
    manifest = load_manifest(run_id, root=runs_root)
    if getattr(manifest.consent, field) is None:
        setattr(manifest.consent, field, now if now is not None else utc_now())
        save_manifest(manifest, root=runs_root)
    return manifest


def missing_moments(manifest: RunManifest, moments: tuple[str, ...]) -> list[str]:
    """Which of ``moments`` this run has no timestamp for."""
    return [moment for moment in moments if getattr(manifest.consent, _FIELDS[moment]) is None]


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Record one consent moment in the run manifest")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--moment", required=True, choices=MOMENTS)
    parser.add_argument("--runs-root", type=Path, default=None, help="test override")
    arguments = parser.parse_args(argv)
    manifest = record_consent(arguments.run_id, arguments.moment, runs_root=arguments.runs_root)
    consent = manifest.consent
    sys.stdout.write(
        json.dumps(
            {
                "moment": arguments.moment,
                "recorded_at": getattr(consent, _FIELDS[arguments.moment]).isoformat(),
                "still_missing": missing_moments(manifest, MOMENTS),
            },
            indent=2,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""The project-wide consent policy version and the gates that enforce it.

Every consent surface — the run manifest's :class:`ConsentState`, the review
page copy, and the submission request envelope — records this version. Any
change to consent wording or privacy behavior bumps it, which invalidates
remembered local-scan consent (specification, 2.2) and appears in checkpoint
compatibility fingerprints.

A recorded consent that nobody checks is decoration. Local-scan consent is
checked where source files are read (``pipeline.collect``); provider-transfer
consent is checked here, by the stages that write the user's own sentences into
files whose only purpose is to be handed to an AI provider.
"""

from pathlib import Path

from glite_english_audit.state.run_store import load_manifest

CONSENT_POLICY_VERSION = "1"


class MissingConsentError(Exception):
    """A step that needs a recorded consent ran on a run that lacks it."""


def require_provider_transfer_consent(run_id: str, *, runs_root: Path | None = None) -> None:
    """Refuse to prepare provider-bound text without this run's own consent.

    Provider transfer is the one step of the audit that is not local, and
    specification 2.2 makes its consent per-run: it is never inferred from a
    previous audit and never implied by the local-scan confirmation. So the
    check reads this run's manifest and refuses when the timestamp is absent —
    including when the manifest is missing, since a run with no recorded state
    has recorded no agreement either.
    """
    manifest = load_manifest(run_id, root=runs_root)
    if manifest.consent.provider_transfer_confirmed_at is None:
        msg = (
            "this run has no recorded provider-transfer consent, so its text must not be "
            "prepared for an AI provider; ask the user on this run and record it first"
        )
        raise MissingConsentError(msg)

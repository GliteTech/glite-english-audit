"""Content-free capability detection for optional direct API submission.

The fixed website report form and package download do not depend on this
configuration. Detection controls only the additional direct API action. It
reads configured contract metadata; it never probes arbitrary endpoints or
sends package content.
"""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from glite_english_audit.artifacts.submission import SUBMISSION_SCHEMA_VERSION

ENDPOINT_CONFIG_NAME = "submission-endpoint.json"


class EndpointConfig(BaseModel):
    """Operator-provided description of a Glite submission endpoint."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    base_url: str
    advertised_schema_versions: list[int] = Field(min_length=1)


class SubmissionCapability(BaseModel):
    """Whether the review page may add its optional direct API action."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    direct_submission_available: bool
    reason: str
    endpoint_base_url: str | None = None


def detect_capability(config_dir: Path) -> SubmissionCapability:
    """Decide between direct submission and download-only, content-free."""
    config_path = config_dir / ENDPOINT_CONFIG_NAME
    if not config_path.is_file():
        return SubmissionCapability(
            direct_submission_available=False,
            reason="No Glite submission endpoint is configured. "
            "Save the package and upload it later on the Glite website.",
        )
    try:
        config = EndpointConfig.model_validate_json(config_path.read_text(encoding="utf-8"))
    except ValueError:
        return SubmissionCapability(
            direct_submission_available=False,
            reason="The configured submission endpoint description is invalid. "
            "Save the package and upload it later on the Glite website.",
        )
    if SUBMISSION_SCHEMA_VERSION not in config.advertised_schema_versions:
        return SubmissionCapability(
            direct_submission_available=False,
            reason="The configured endpoint does not accept this package version. "
            "Save the package and upload it later on the Glite website.",
        )
    if not config.base_url.startswith("https://"):
        return SubmissionCapability(
            direct_submission_available=False,
            reason="The configured endpoint is not an https URL, so direct submission "
            "is disabled. Save the package and upload it manually.",
        )
    return SubmissionCapability(
        direct_submission_available=True,
        reason="A compatible Glite endpoint is configured.",
        endpoint_base_url=config.base_url,
    )

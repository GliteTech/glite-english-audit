"""Content-free capability detection for direct submission."""

import json
from pathlib import Path

from glite_english_audit.artifacts.submission import SUBMISSION_SCHEMA_VERSION
from glite_english_audit.submission.capability import (
    ENDPOINT_CONFIG_NAME,
    detect_capability,
)


def _write_config(config_dir: Path, payload: object) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    text = payload if isinstance(payload, str) else json.dumps(payload)
    (config_dir / ENDPOINT_CONFIG_NAME).write_text(text, encoding="utf-8")


def test_no_config_file_means_download_only(tmp_path: Path) -> None:
    capability = detect_capability(tmp_path)
    assert capability.direct_submission_available is False
    assert capability.endpoint_base_url is None
    assert "upload" in capability.reason.lower()


def test_invalid_json_config_means_download_only(tmp_path: Path) -> None:
    _write_config(tmp_path, "this is { not json")
    capability = detect_capability(tmp_path)
    assert capability.direct_submission_available is False
    assert capability.endpoint_base_url is None
    assert "invalid" in capability.reason.lower()


def test_config_with_extra_field_means_download_only(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {
            "base_url": "https://glite.example",
            "advertised_schema_versions": [SUBMISSION_SCHEMA_VERSION],
            "api_key": "sk-FAKEFAKEFAKE0000",
        },
    )
    capability = detect_capability(tmp_path)
    assert capability.direct_submission_available is False


def test_schema_version_mismatch_means_download_only(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {
            "base_url": "https://glite.example",
            "advertised_schema_versions": [SUBMISSION_SCHEMA_VERSION + 1],
        },
    )
    capability = detect_capability(tmp_path)
    assert capability.direct_submission_available is False
    assert capability.endpoint_base_url is None
    assert "version" in capability.reason.lower()


def test_non_https_endpoint_means_download_only(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {
            "base_url": "http://glite.example",
            "advertised_schema_versions": [SUBMISSION_SCHEMA_VERSION],
        },
    )
    capability = detect_capability(tmp_path)
    assert capability.direct_submission_available is False
    assert capability.endpoint_base_url is None
    assert "https" in capability.reason


def test_valid_https_config_enables_direct_submission(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {
            "base_url": "https://glite.example",
            "advertised_schema_versions": [
                SUBMISSION_SCHEMA_VERSION,
                SUBMISSION_SCHEMA_VERSION + 1,
            ],
        },
    )
    capability = detect_capability(tmp_path)
    assert capability.direct_submission_available is True
    assert capability.endpoint_base_url == "https://glite.example"
    assert capability.reason

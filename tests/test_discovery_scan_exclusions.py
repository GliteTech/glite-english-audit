"""A filename scan must never ingest this project's own synthetic data."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from glite_english_audit.adapters.aider import create_adapter
from glite_english_audit.artifacts.enums import OsEnvironment
from glite_english_audit.discovery.base import DiscoveryContext
from glite_english_audit.discovery.scan_exclusions import (
    audit_owned_roots,
    is_synthetic_fixture_dir,
    should_prune_scan_dir,
)


def test_declared_fixture_dir_is_recognized(tmp_path: Path) -> None:
    (tmp_path / "fixture.json").write_text(
        json.dumps({"adapter_id": "aider", "kind": "success", "synthetic": True}),
        encoding="utf-8",
    )
    assert is_synthetic_fixture_dir(tmp_path)


def test_undeclared_and_malformed_markers_are_not_fixtures(tmp_path: Path) -> None:
    assert not is_synthetic_fixture_dir(tmp_path)
    (tmp_path / "fixture.json").write_text("{not json", encoding="utf-8")
    assert not is_synthetic_fixture_dir(tmp_path)
    (tmp_path / "fixture.json").write_text(json.dumps({"synthetic": False}), encoding="utf-8")
    assert not is_synthetic_fixture_dir(tmp_path)


def test_audit_owned_roots_cover_the_private_runtime_tree(tmp_path: Path) -> None:
    roots = audit_owned_roots(tmp_path)
    assert roots == {tmp_path / "temp", tmp_path / "runtime"}
    assert should_prune_scan_dir(tmp_path / "runtime" / "run-1", audit_roots=roots)
    assert not should_prune_scan_dir(tmp_path / "projects" / "work", audit_roots=roots)


def test_declared_fixture_tree_is_pruned_by_its_marker(tmp_path: Path) -> None:
    variant = tmp_path / "fixtures" / "aider" / "success"
    (variant / "home" / "proj").mkdir(parents=True)
    (variant / "fixture.json").write_text(
        json.dumps({"adapter_id": "aider", "kind": "success", "synthetic": True}),
        encoding="utf-8",
    )
    roots = audit_owned_roots(tmp_path)
    assert should_prune_scan_dir(variant, audit_roots=roots)
    # The marker prunes the whole subtree at its own level, so a scan of a real
    # home never descends into the synthetic home below it.
    assert not should_prune_scan_dir(variant.parent, audit_roots=roots)


def test_scan_of_a_home_containing_a_checkout_skips_its_fixtures(tmp_path: Path) -> None:
    # Regression: scanning a contributor's home directory discovered this
    # project's own aider fixtures and would have ingested synthetic text as
    # if the contributor had written it.
    checkout = tmp_path / "glite-english-audit"
    variant = checkout / "fixtures" / "aider" / "success"
    fixture_project = variant / "home" / "proj-a"
    fixture_project.mkdir(parents=True)
    (variant / "fixture.json").write_text(
        json.dumps({"adapter_id": "aider", "kind": "success", "synthetic": True}),
        encoding="utf-8",
    )
    (fixture_project / ".aider.input.history").write_text(
        "# 2026-08-01 10:00:00.000000\n+synthetic fixture text must never be ingested\n",
        encoding="utf-8",
    )
    real_project = tmp_path / "work" / "demo"
    real_project.mkdir(parents=True)
    (real_project / ".aider.input.history").write_text(
        "# 2026-08-02 11:00:00.000000\n+I very like this plan\n",
        encoding="utf-8",
    )

    outcome = create_adapter(audit_roots=audit_owned_roots(checkout)).discover(
        DiscoveryContext(
            os_environment=OsEnvironment.MACOS,
            home=tmp_path,
            now=datetime.now(tz=UTC),
            environ={},
        )
    )
    discovered = {str(path) for path in outcome.instance_paths.values()}
    assert os.fspath(real_project) in discovered
    assert not [path for path in discovered if "fixtures" in path]


def test_scan_skips_the_private_runtime_tree(tmp_path: Path) -> None:
    checkout = tmp_path / "glite-english-audit"
    snapshot = checkout / "runtime" / "run-1" / "snapshots" / "proj"
    snapshot.mkdir(parents=True)
    (snapshot / ".aider.input.history").write_text(
        "# 2026-08-01 10:00:00.000000\n+snapshot copies must never be re-ingested\n",
        encoding="utf-8",
    )
    outcome = create_adapter(audit_roots=audit_owned_roots(checkout)).discover(
        DiscoveryContext(
            os_environment=OsEnvironment.MACOS,
            home=tmp_path,
            now=datetime.now(tz=UTC),
            environ={},
        )
    )
    assert not [r for r in outcome.records if r.candidate_messages > 0]


def test_scan_still_finds_history_outside_audit_roots(tmp_path: Path) -> None:
    project = tmp_path / "projects" / "demo"
    project.mkdir(parents=True)
    (project / ".aider.input.history").write_text(
        "# 2026-08-01 10:00:00.000000\n+I very like this plan\n",
        encoding="utf-8",
    )
    outcome = create_adapter(audit_roots=audit_owned_roots(tmp_path)).discover(
        DiscoveryContext(
            os_environment=OsEnvironment.MACOS,
            home=tmp_path,
            now=datetime.now(tz=UTC),
            environ={},
        )
    )
    found = [r for r in outcome.records if r.candidate_messages > 0]
    assert len(found) == 1
    assert os.fspath(project) in [str(p) for p in outcome.instance_paths.values()]

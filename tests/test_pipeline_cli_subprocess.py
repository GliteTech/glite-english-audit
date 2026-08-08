"""Stage drivers must work in a fresh process, not only inside pytest.

Every other pipeline test imports the driver and calls it, which inherits
whatever global state earlier tests set up. A user runs `uv run python -m ...`,
where nothing is set up. That difference hid a real defect: `collect` never
registered the adapters, so every source failed with a bare KeyError and the
run silently collected nothing — while the in-process test passed, because its
own fixture had registered an adapter first.
"""

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from glite_english_audit.adapters.claude_code import create_adapter as claude_code_adapter
from glite_english_audit.artifacts.enums import OsEnvironment
from glite_english_audit.artifacts.io import ensure_private_dir, write_model
from glite_english_audit.discovery.base import DiscoveryContext
from glite_english_audit.discovery.inventory import PrivateInventory

_REPO = Path(__file__).resolve().parent.parent
_FIXTURE_HOME = _REPO / "fixtures" / "claude_code" / "success" / "home"


def _run(module: str, *args: str) -> dict[str, object]:
    """Invoke a stage driver exactly as a user would, and parse its output."""
    result = subprocess.run(
        [sys.executable, "-m", module, *args],
        cwd=_REPO,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"{module} failed: {result.stderr[-2000:]}"
    return dict(json.loads(result.stdout))


def _seed_inventory(inventory_dir: Path) -> None:
    outcome = claude_code_adapter().discover(
        DiscoveryContext(
            os_environment=OsEnvironment.MACOS,
            home=_FIXTURE_HOME,
            now=datetime(2026, 8, 8, tzinfo=UTC),
            environ={},
        )
    )
    ensure_private_dir(inventory_dir)
    write_model(
        inventory_dir / "source-inventory.json",
        PrivateInventory(
            records=outcome.records,
            instance_paths={key: str(path) for key, path in outcome.instance_paths.items()},
        ),
    )


def test_start_run_and_collect_work_in_a_fresh_process(tmp_path: Path) -> None:
    inventory_dir = tmp_path / "inventory"
    _seed_inventory(inventory_dir)
    runs_root = tmp_path / "runs"

    started = _run(
        "glite_english_audit.pipeline.start_run",
        "--inventory-dir",
        str(inventory_dir),
        "--runs-root",
        str(runs_root),
        "--period",
        "everything",
    )
    run_id = str(started["run_id"])
    assert started["selected_instances"]

    collected = _run(
        "glite_english_audit.pipeline.collect",
        "--run-id",
        run_id,
        "--runs-root",
        str(runs_root),
        "--repo",
        str(_REPO),
    )
    # The defect this test exists for: adapters unregistered in a fresh process
    # made every instance fail and the count silently fall to zero.
    assert collected["excluded_instances"] == [], collected["excluded_instances"]
    assert int(str(collected["candidate_utterances"])) > 0


def test_discovery_cli_reports_registered_adapters_in_a_fresh_process(tmp_path: Path) -> None:
    payload = _run(
        "glite_english_audit.discovery.inventory",
        "--run-dir",
        str(tmp_path / "inv"),
    )
    rows = payload["inventory"]
    assert isinstance(rows, list)
    # Every shipped adapter reports something, even if only "not found": a
    # missing adapter means the registry did not load in this process.
    reported = {str(row["adapter_id"]) for row in rows}
    assert {
        "aider",
        "claude_code",
        "cline",
        "codex",
        "cursor",
        "gemini_cli",
        "opencode",
        "roo_code",
        "wispr_flow",
    } <= reported

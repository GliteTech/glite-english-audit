"""The synthetic fixture framework: layout, metadata, and policy checks.

Every fixture directory under ``fixtures/<adapter_id>/<variant>/`` carries a
``fixture.json`` declaring what it represents and attesting that it is
synthetic (specification, 13.1). A repository-wide test walks the tree and
fails when a fixture is undeclared, claims to be real, or contains
secret-looking values that are not unmistakably fake.
"""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from glite_english_audit.diagnostics.codes import Diagnostic

FIXTURE_META_NAME = "fixture.json"

# Fake credentials in fixtures must contain one of these markers so no scanner
# or human can mistake them for a real secret.
FAKE_SECRET_MARKERS = ("FAKE", "SYNTHETIC", "EXAMPLE", "PLACEHOLDER")


class FixtureMeta(BaseModel):
    """Declaration accompanying every fixture directory."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    adapter_id: str
    kind: Literal["success", "empty", "malformed", "unsupported", "migration", "unit"]
    description: str
    synthetic: Literal[True]
    storage_variant: str | None = None


def fixtures_root(repo_root: Path) -> Path:
    """The committed fixtures directory."""
    return repo_root / "fixtures"


def iter_fixture_dirs(root: Path) -> list[Path]:
    """Every directory that directly contains a fixture.json."""
    if not root.is_dir():
        return []
    return sorted(path.parent for path in root.rglob(FIXTURE_META_NAME))


def load_fixture_meta(directory: Path) -> FixtureMeta:
    """Read and validate one fixture declaration."""
    meta_path = directory / FIXTURE_META_NAME
    return FixtureMeta.model_validate_json(meta_path.read_text(encoding="utf-8"))


def check_fixture_tree(repo_root: Path) -> list[Diagnostic]:
    """Policy check for the whole fixtures tree."""
    diagnostics: list[Diagnostic] = []
    root = fixtures_root(repo_root)
    declared = set(iter_fixture_dirs(root))
    if not root.is_dir():
        return diagnostics
    for entry in sorted(root.rglob("*")):
        if not entry.is_file() or entry.name == FIXTURE_META_NAME:
            continue
        if not any(parent in declared for parent in (entry.parent, *entry.parent.parents)):
            diagnostics.append(
                Diagnostic.from_code(
                    "SCHEMA_MISSING_FIELD",
                    "fixture file has no fixture.json declaration in any parent directory: "
                    f"{entry.relative_to(repo_root)}",
                    item_ref=str(entry.relative_to(repo_root)),
                )
            )
    for directory in declared:
        try:
            load_fixture_meta(directory)
        except ValueError:
            diagnostics.append(
                Diagnostic.from_code(
                    "SCHEMA_INVALID_VALUE",
                    f"fixture.json fails validation: {directory.relative_to(repo_root)}",
                    item_ref=str(directory.relative_to(repo_root)),
                )
            )
    return diagnostics

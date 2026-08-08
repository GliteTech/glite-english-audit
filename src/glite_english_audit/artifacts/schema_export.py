"""Generate committed JSON Schemas from the authoritative Pydantic models.

Committed JSON Schema exists only for language-neutral boundaries consumed
outside Python: the downloadable submission package and the request/response
contract shared with the TypeScript website (specification, 5.1). CI
regenerates these files and fails when the committed copies differ.

Run: ``uv run python -m glite_english_audit.artifacts.schema_export``
Check: add ``--check`` to fail without writing when files would change.
"""

import json
import sys
from pathlib import Path

from pydantic import BaseModel

from glite_english_audit.artifacts.submission import (
    NewSubmissionRequest,
    ReportLookupRequest,
    SubmissionAccepted,
    SubmissionPackage,
    SubmissionRejected,
)
from glite_english_audit.paths import repo_root

EXPORTED_MODELS: dict[str, type[BaseModel]] = {
    "submission_package": SubmissionPackage,
    "new_submission_request": NewSubmissionRequest,
    "report_lookup_request": ReportLookupRequest,
    "submission_accepted": SubmissionAccepted,
    "submission_rejected": SubmissionRejected,
}


def schemas_dir() -> Path:
    """The committed schema directory."""
    return repo_root() / "schemas"


def render_schema(model_type: type[BaseModel]) -> str:
    """Deterministic pretty JSON Schema text for one model."""
    schema = model_type.model_json_schema()
    return json.dumps(schema, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def export_schemas(target_dir: Path, *, check: bool) -> list[str]:
    """Write (or verify) every exported schema. Returns drifted file names."""
    drifted: list[str] = []
    target_dir.mkdir(parents=True, exist_ok=True)
    for name, model_type in EXPORTED_MODELS.items():
        path = target_dir / f"{name}.schema.json"
        rendered = render_schema(model_type)
        existing = path.read_text(encoding="utf-8") if path.exists() else None
        if existing != rendered:
            drifted.append(path.name)
            if not check:
                path.write_text(rendered, encoding="utf-8")
    return drifted


def main(argv: list[str]) -> int:
    """CLI entry point."""
    check = "--check" in argv
    drifted = export_schemas(schemas_dir(), check=check)
    if check and drifted:
        sys.stderr.write(
            "committed JSON Schemas drift from the Pydantic models: "
            + ", ".join(sorted(drifted))
            + "\nRun: uv run python -m glite_english_audit.artifacts.schema_export\n"
        )
        return 1
    if drifted:
        sys.stdout.write("updated: " + ", ".join(sorted(drifted)) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

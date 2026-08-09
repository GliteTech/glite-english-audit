"""CLI: deterministic skill verifier.

Run: ``uv run python -m glite_english_audit.verification.verify_skills``
Exits non-zero when any error-level diagnostic is found. Semantic skill review
(clarity, contradictions, examples) is a separate agent-run check; this CLI
covers everything checkable without model judgment.
"""

import sys

from glite_english_audit.diagnostics.codes import Severity
from glite_english_audit.paths import repo_root
from glite_english_audit.verification.skills import verify_all_skills


def main() -> int:
    """Verify every canonical skill and generated wrapper."""
    diagnostics = verify_all_skills(repo_root())
    for diagnostic in diagnostics:
        stream = sys.stderr if diagnostic.severity is Severity.ERROR else sys.stdout
        stream.write(f"{diagnostic.severity.value}: {diagnostic.code}: {diagnostic.message}\n")
    errors = [d for d in diagnostics if d.severity is Severity.ERROR]
    if errors:
        noun = "error" if len(errors) == 1 else "errors"
        sys.stderr.write(f"skill verification failed with {len(errors)} {noun}\n")
        return 1
    sys.stdout.write("skill verification passed\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

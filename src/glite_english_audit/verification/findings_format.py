"""Deterministic validator for the stage-4 plain-findings format.

The exact layout is defined in ``specifications/artifacts.md``, Section 4:
a fixed title and threshold statement, then either numbered finding blocks
(Original / Correction / Why, optional Uncertainty) or the exact empty-result
sentence. This validator enforces the layout and the sidecar invariants
without any model judgment.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from glite_english_audit.artifacts.hashing import sha256_hex
from glite_english_audit.artifacts.models import FindingsArtifactMeta
from glite_english_audit.diagnostics.codes import Diagnostic

TITLE_LINE = "# English findings"
THRESHOLD_LINE = (
    "Threshold: this audit reports only constructions that strongly suggest non-native "
    "English. Slips, chat shorthand, and native-plausible informal usage are not reported."
)
EMPTY_RESULT_LINE = "No high-confidence mistakes were found."

_HEADING = re.compile(r"^## Finding (\d+)$")


@dataclass(frozen=True)
class ParsedFindings:
    """Structure extracted from a valid findings body."""

    finding_count: int
    no_mistakes_found: bool


def validate_findings_body(
    text: str, *, item_ref: str
) -> tuple[ParsedFindings | None, list[Diagnostic]]:
    """Validate one findings body against the deterministic format."""

    def fail(message: str) -> tuple[None, list[Diagnostic]]:
        return None, [Diagnostic.from_code("SCHEMA_INVALID_VALUE", message, item_ref=item_ref)]

    if not text.endswith("\n") or text.endswith("\n\n"):
        return fail("findings file must end with exactly one trailing newline")
    lines = text.split("\n")[:-1]
    if len(lines) < 5:
        return fail("findings file is too short for the required layout")
    if lines[0] != TITLE_LINE:
        return fail(f"line 1 must be {TITLE_LINE!r}")
    if lines[1] != "":
        return fail("line 2 must be blank")
    if lines[2] != THRESHOLD_LINE:
        return fail("line 3 must be the exact threshold statement")
    if lines[3] != "":
        return fail("line 4 must be blank")

    body = lines[4:]
    if body == [EMPTY_RESULT_LINE]:
        return ParsedFindings(finding_count=0, no_mistakes_found=True), []

    diagnostics: list[Diagnostic] = []
    index = 0
    block_number = 0
    while index < len(body):
        heading = _HEADING.match(body[index])
        if heading is None:
            return fail(f"expected '## Finding {block_number + 1}' at body line {index + 1}")
        block_number += 1
        if int(heading.group(1)) != block_number:
            return fail(f"finding blocks must be numbered without gaps; got {body[index]!r}")
        block = body[index + 1 :]
        if len(block) < 4 or block[0] != "":
            return fail(f"finding {block_number} must start with one blank line")
        if not block[1].startswith("Original: "):
            return fail(f"finding {block_number} is missing its 'Original: ' line")
        if not block[2].startswith("Correction: "):
            return fail(f"finding {block_number} is missing its 'Correction: ' line")
        if not block[3].startswith("Why: "):
            return fail(f"finding {block_number} is missing its 'Why: ' line")
        consumed = 5
        if len(block) > 4 and block[4].startswith("Uncertainty: "):
            consumed = 6
        index += consumed
        if index < len(body):
            if body[index] != "":
                return fail(f"finding {block_number} must be followed by one blank line")
            index += 1
    if block_number == 0:
        return fail("findings file has neither finding blocks nor the empty-result sentence")
    return ParsedFindings(finding_count=block_number, no_mistakes_found=False), diagnostics


def verify_findings_artifact(
    body_path: Path, meta: FindingsArtifactMeta, *, item_ref: str
) -> list[Diagnostic]:
    """Validate a findings file against its sidecar's invariants."""
    diagnostics: list[Diagnostic] = []
    if not body_path.is_file():
        return [
            Diagnostic.from_code(
                "LINEAGE_MISSING_INPUT",
                f"findings body file is missing: {item_ref}",
                item_ref=item_ref,
            )
        ]
    raw = body_path.read_bytes()
    if sha256_hex(raw) != meta.body_sha256:
        diagnostics.append(
            Diagnostic.from_code(
                "LINEAGE_HASH_MISMATCH",
                "findings body bytes do not match the sidecar hash",
                item_ref=item_ref,
            )
        )
    parsed, format_diagnostics = validate_findings_body(raw.decode("utf-8"), item_ref=item_ref)
    diagnostics.extend(format_diagnostics)
    if parsed is not None:
        if parsed.finding_count != meta.finding_count:
            diagnostics.append(
                Diagnostic.from_code(
                    "CARDINALITY_MISMATCH",
                    f"sidecar claims {meta.finding_count} finding(s) but the body has "
                    f"{parsed.finding_count}",
                    item_ref=item_ref,
                )
            )
        if parsed.no_mistakes_found != meta.no_mistakes_found:
            diagnostics.append(
                Diagnostic.from_code(
                    "SCHEMA_INVALID_VALUE",
                    "sidecar no_mistakes_found flag disagrees with the body form",
                    item_ref=item_ref,
                )
            )
    return diagnostics

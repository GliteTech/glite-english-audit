"""Frozen contract versions must match the generated schemas exactly."""

import re
from pathlib import Path

from glite_english_audit.artifacts.hashing import sha256_hex

_REPO = Path(__file__).resolve().parent.parent
_DOC = _REPO / "specifications" / "contract_versions.md"
_ROW = re.compile(r"^\| `(schemas/[\w.]+)` \| `([0-9a-f]{64})` \|$", re.MULTILINE)


def test_every_frozen_digest_matches_the_generated_schema() -> None:
    text = _DOC.read_text(encoding="utf-8")
    rows = _ROW.findall(text)
    assert len(rows) == 5, "version 1 must freeze all five schema files"
    for relative_path, digest in rows:
        actual = sha256_hex((_REPO / relative_path).read_bytes())
        assert actual == digest, (
            f"{relative_path} drifted from frozen contract version 1; a schema change "
            "requires a new submission_schema_version and a new frozen row"
        )

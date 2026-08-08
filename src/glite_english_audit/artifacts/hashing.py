"""Canonical JSON serialization and hashing for artifact lineage.

Every hash in the project is a SHA-256 hex digest over canonical JSON bytes:
UTF-8, sorted keys, compact separators, no ASCII escaping. Canonicalization is
deliberately simple so an independent implementation (for example, the website
in TypeScript) can reproduce it exactly.
"""

import hashlib
import json
import secrets
import uuid
from typing import Any

from pydantic import BaseModel


def canonical_json_bytes(data: dict[str, Any]) -> bytes:
    """Serialize ``data`` as canonical JSON bytes."""
    return json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    """Return the SHA-256 hex digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


def model_canonical_hash(model: BaseModel, *, exclude: set[str] | None = None) -> str:
    """Hash a Pydantic model over its canonical JSON form.

    ``exclude`` removes top-level fields before hashing, for example, the
    payload-hash field itself when computing a package hash.
    """
    dumped = model.model_dump(mode="json", exclude=exclude)
    return sha256_hex(canonical_json_bytes(dumped))


def new_run_id() -> str:
    """Return a new unique run identifier."""
    return f"run-{uuid.uuid4().hex}"


def new_artifact_id() -> str:
    """Return a new unique artifact identifier."""
    return f"art-{uuid.uuid4().hex}"


def new_submission_id() -> str:
    """Return a new random idempotent submission identifier."""
    return f"sub-{uuid.uuid4().hex}"


def new_recovery_secret() -> str:
    """Return a cryptographically random 256-bit secret as 64 hex characters."""
    return secrets.token_hex(32)

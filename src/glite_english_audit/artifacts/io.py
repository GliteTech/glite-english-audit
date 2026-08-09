"""Atomic, owner-only file IO for private runtime artifacts.

Writes go to a temporary file in the destination directory, are flushed and
fsynced, then atomically renamed over the target. On POSIX systems every
private file is created with mode 0600 and every directory with 0700
(specification, 3.6). Windows ACL tightening is handled by the platform
adapter layer and is a documented platform-specific exception.
"""

import json
import os
import stat
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path

from pydantic import BaseModel

_POSIX = os.name == "posix"


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` (and any missing parents) as owner-only directories.

    Only directories this call actually creates are tightened to mode 0700;
    pre-existing parents such as ``~/Library`` are never touched.
    """
    missing: list[Path] = []
    current = path
    while not current.exists() and current != current.parent:
        missing.append(current)
        current = current.parent
    path.mkdir(parents=True, exist_ok=True)
    if _POSIX:
        for directory in missing:
            directory.chmod(stat.S_IRWXU)
    return path


def restrict_private_file(path: Path) -> None:
    """Tighten an existing private file to mode 0600 on POSIX.

    For files written outside :func:`atomic_write_bytes` — snapshot copies,
    which are streamed rather than built in memory. A copy inherits nothing
    useful from its source's mode, and a source database is often world
    readable.
    """
    if _POSIX:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically with owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if _POSIX:
            tmp_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


def atomic_write_text(path: Path, text: str) -> None:
    """Write UTF-8 text atomically."""
    atomic_write_bytes(path, text.encode("utf-8"))


def write_model(path: Path, model: BaseModel) -> None:
    """Serialize a Pydantic model to pretty JSON and write it atomically."""
    payload = json.dumps(model.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"
    atomic_write_text(path, payload)


def read_model[ModelT: BaseModel](path: Path, model_type: type[ModelT]) -> ModelT:
    """Read and validate a JSON file against ``model_type``."""
    return model_type.model_validate_json(path.read_text(encoding="utf-8"))


def write_jsonl_models(path: Path, models: Iterable[BaseModel]) -> int:
    """Write models as JSONL atomically. Returns the number of lines."""
    lines: list[str] = []
    for model in models:
        lines.append(json.dumps(model.model_dump(mode="json"), ensure_ascii=False))
    payload = "\n".join(lines) + ("\n" if lines else "")
    atomic_write_text(path, payload)
    return len(lines)


def read_jsonl_models[ModelT: BaseModel](path: Path, model_type: type[ModelT]) -> Iterator[ModelT]:
    """Stream and validate JSONL records against ``model_type``."""
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped:
                yield model_type.model_validate_json(stripped)

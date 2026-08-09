"""What model and effort the current agent session is actually running.

The calibration profile is keyed by model and effort, and nothing checked
either. On the machine this was written, the profile assumed ``claude-fable-5``
at ``medium`` while the session ran ``claude-opus-5`` at ``xhigh`` — so the
hours and tokens shown to the user were measured on something other than what
would do the work, with no mention of it.

Both facts are available locally and for free. Effort arrives in the process
environment. The model appears on the session transcript's assistant records,
which is the user's own file on the user's own machine.

Privacy rule for the transcript read: only the ``model`` field is extracted, and
only an identifier is returned. Lines are scanned from the end and the scan
stops at the first model found, so the common case touches one line. No message
text is retained, logged, or returned, and nothing here reaches a model or the
network.

Detection is best-effort by design. Every function returns ``None`` rather than
guessing, because a wrong model identifier would silently select the wrong
calibration cell — worse than the unknown it replaced.
"""

import json
import os
from collections.abc import Mapping
from pathlib import Path

# The transcript is JSONL, one record per line, newest last. A session that has
# run for hours can be tens of megabytes, so the tail is read rather than the
# file. This is generous for finding the most recent assistant record while
# staying far below the cost of a full parse.
_TAIL_BYTES = 256 * 1024


def detect_effort(*, environ: Mapping[str, str] | None = None) -> str | None:
    """The reasoning effort of the current session, or ``None``.

    Claude Code hands this to every child process, so no file is read.
    """
    source = environ if environ is not None else os.environ
    value = source.get("CLAUDE_EFFORT", "").strip().lower()
    return value or None


def _session_transcript(environ: Mapping[str, str], home: Path) -> Path | None:
    session_id = environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    if not session_id or "/" in session_id or "\\" in session_id or ".." in session_id:
        # The identifier is joined into a path, so an odd one is refused rather
        # than resolved. A session whose ID is not a plain name is not a session
        # this function knows how to find.
        return None
    matches = sorted((home / ".claude" / "projects").glob(f"*/{session_id}.jsonl"))
    return matches[0] if matches else None


def detect_model(
    *, environ: Mapping[str, str] | None = None, home: Path | None = None
) -> str | None:
    """The model the current session is using, or ``None``.

    Read from the tail of the session transcript. Returns the identifier only.
    """
    source = environ if environ is not None else os.environ
    root = home if home is not None else Path.home()
    transcript = _session_transcript(source, root)
    if transcript is None or not transcript.is_file():
        return None
    try:
        size = transcript.stat().st_size
        with transcript.open("rb") as handle:
            if size > _TAIL_BYTES:
                handle.seek(size - _TAIL_BYTES)
                handle.readline()  # discard the partial line the seek landed in
            lines = handle.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None

    for line in reversed(lines):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        model = message.get("model")
        # A synthetic placeholder is not a model that ran anything.
        if isinstance(model, str) and model and not model.startswith("<"):
            return model
    return None

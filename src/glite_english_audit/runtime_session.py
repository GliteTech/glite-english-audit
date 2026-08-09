"""What model and effort the current agent session is actually running.

**The model is inherited, never chosen.** The per-file agents of steps c, d and
e run on whatever model the session that launched them is running. Nothing here
pins one, nothing here can, and nothing will: this repository makes no
inference call, so there is no place to pin a model on. Every statement the
product makes about which model reads the learner's writing is therefore an
observation of the running session — and this module is the only thing that can
make it.

The calibration profile is keyed by model and effort, and nothing checked
either. On the machine this was written, the profile assumed ``claude-fable-5``
at ``medium`` while the session ran ``claude-opus-5`` at ``xhigh`` — so the
hours and tokens shown to the user were measured on something other than what
would do the work, with no mention of it. The profile stayed a description of
what was measured; what changed is that the product stopped reading it as a
statement about this run.

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

UNKNOWN_SESSION_VALUE = "<unknown>"
"""Recorded where detection returned nothing.

Deliberately a value :func:`detect_model` can never return: it refuses any
identifier starting with ``<`` as a synthetic placeholder, so a real model can
never collide with the sentinel and nobody reading a manifest can mistake one
for the other.
"""

SESSION_MODEL_KEY = "session-model"
SESSION_EFFORT_KEY = "session-effort"


def detect_effort(*, environ: Mapping[str, str] | None = None) -> str | None:
    """The reasoning effort of the current session, or ``None``.

    Claude Code hands this to every child process, so no file is read.
    """
    source = environ if environ is not None else os.environ
    value = source.get("CLAUDE_EFFORT", "").strip().lower()
    if not value or value.startswith("<"):
        # Same refusal as `detect_model`, so the sentinel this module records
        # for an unreadable session can never be mistaken for something read.
        # A guarantee that holds for one of the two keys is not a guarantee.
        return None
    return value


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


def observed_model_ids(
    *, environ: Mapping[str, str] | None = None, home: Path | None = None
) -> dict[str, str]:
    """What this session is running, in the shape the run manifest freezes.

    Two entries, because the semantic steps share one inherited configuration
    rather than choosing three: ``session-model`` and ``session-effort``. Both
    are observations. Where detection returns nothing,
    :data:`UNKNOWN_SESSION_VALUE` is recorded rather than a substitute, because
    resume compares this map and invalidates from the first semantic step when
    it differs — a guess written here would silently reuse judgments made by
    another model.

    What an unknown compares as on resume: ``<unknown>`` equals ``<unknown>``,
    so a run recorded blind still resumes, and it never equals a named model,
    so a session that became readable recomputes the semantic steps. Making it
    compare unequal to itself was the other candidate; it would end resume
    entirely for every host this cannot read — Codex sessions among them —
    while proving nothing, since two failed detections say nothing about
    whether the model changed between them.
    """
    return {
        SESSION_MODEL_KEY: detect_model(environ=environ, home=home) or UNKNOWN_SESSION_VALUE,
        SESSION_EFFORT_KEY: detect_effort(environ=environ) or UNKNOWN_SESSION_VALUE,
    }

"""What model and effort the session is running, so the estimate can say so.

The calibration profile is keyed by model and effort and nothing compared
either against the session. On the machine this was written the profile assumed
claude-fable-5 at medium while the session ran claude-opus-5 at xhigh, so every
hour and token shown described a run the user would not get, silently.
"""

import json
from pathlib import Path

from glite_english_audit.runtime_session import (
    SESSION_EFFORT_KEY,
    SESSION_MODEL_KEY,
    UNKNOWN_SESSION_VALUE,
    detect_effort,
    detect_model,
    observed_model_ids,
)

_SESSION = "0193a1b2-0000-7000-8000-000000000001"


def _transcript(home: Path, records: list[dict[str, object]]) -> Path:
    directory = home / ".claude" / "projects" / "some-project"
    directory.mkdir(parents=True)
    path = directory / f"{_SESSION}.jsonl"
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    return path


def _env(**overrides: str) -> dict[str, str]:
    base = {"CLAUDE_CODE_SESSION_ID": _SESSION}
    base.update(overrides)
    return base


def test_effort_comes_from_the_environment(tmp_path: Path) -> None:
    assert detect_effort(environ={"CLAUDE_EFFORT": "xhigh"}) == "xhigh"


def test_an_absent_effort_is_unknown_rather_than_guessed() -> None:
    assert detect_effort(environ={}) is None
    assert detect_effort(environ={"CLAUDE_EFFORT": "  "}) is None


def test_the_model_is_read_from_the_session_transcript(tmp_path: Path) -> None:
    _transcript(
        tmp_path,
        [
            {"message": {"role": "assistant", "model": "claude-fable-5", "content": "x"}},
            {"message": {"role": "assistant", "model": "claude-opus-5", "content": "y"}},
        ],
    )
    # The newest record wins: a session that switched models is running the
    # one it switched to.
    assert detect_model(environ=_env(), home=tmp_path) == "claude-opus-5"


def test_a_synthetic_placeholder_is_not_a_model(tmp_path: Path) -> None:
    _transcript(
        tmp_path,
        [
            {"message": {"role": "assistant", "model": "claude-opus-5"}},
            {"message": {"role": "assistant", "model": "<synthetic>"}},
        ],
    )
    assert detect_model(environ=_env(), home=tmp_path) == "claude-opus-5"


def test_a_missing_transcript_is_unknown(tmp_path: Path) -> None:
    assert detect_model(environ=_env(), home=tmp_path) is None


def test_no_session_id_is_unknown(tmp_path: Path) -> None:
    assert detect_model(environ={}, home=tmp_path) is None


def test_a_path_shaped_session_id_is_refused(tmp_path: Path) -> None:
    # The identifier is joined into a glob, so an odd one is refused rather
    # than resolved.
    for hostile in ("../../etc/passwd", "a/b", "..", "x\\y"):
        assert detect_model(environ={"CLAUDE_CODE_SESSION_ID": hostile}, home=tmp_path) is None


def test_a_corrupt_line_does_not_stop_the_scan(tmp_path: Path) -> None:
    _transcript(tmp_path, [{"message": {"role": "assistant", "model": "claude-opus-5"}}])
    path = tmp_path / ".claude" / "projects" / "some-project" / f"{_SESSION}.jsonl"
    path.write_text(path.read_text(encoding="utf-8") + "{not json\n", encoding="utf-8")
    assert detect_model(environ=_env(), home=tmp_path) == "claude-opus-5"


def test_only_the_tail_of_a_large_transcript_is_read(tmp_path: Path) -> None:
    """A session running for hours writes tens of megabytes.

    Reading the whole file to find one identifier would make every estimate
    wait on it, and would pull far more of the user's writing through memory
    than the one field this needs.
    """
    filler: dict[str, object] = {
        "message": {"role": "assistant", "model": "claude-fable-5", "content": "z" * 2000}
    }
    records: list[dict[str, object]] = [filler] * 400
    records.append({"message": {"role": "assistant", "model": "claude-opus-5"}})
    path = _transcript(tmp_path, records)
    assert path.stat().st_size > 256 * 1024, "the fixture must exceed the tail window"
    assert detect_model(environ=_env(), home=tmp_path) == "claude-opus-5"


def test_a_record_without_a_message_is_skipped(tmp_path: Path) -> None:
    _transcript(
        tmp_path,
        [
            {"message": {"role": "assistant", "model": "claude-opus-5"}},
            {"type": "summary", "summary": "something"},
            {"message": "not an object"},
        ],
    )
    assert detect_model(environ=_env(), home=tmp_path) == "claude-opus-5"


def test_the_manifest_shape_reports_the_session_it_observed(tmp_path: Path) -> None:
    _transcript(tmp_path, [{"message": {"role": "assistant", "model": "claude-opus-5"}}])
    observed = observed_model_ids(environ=_env(CLAUDE_EFFORT="xhigh"), home=tmp_path)
    assert observed == {SESSION_MODEL_KEY: "claude-opus-5", SESSION_EFFORT_KEY: "xhigh"}


def test_what_cannot_be_read_is_recorded_as_unknown(tmp_path: Path) -> None:
    """Not substituted, and not omitted.

    Resume compares this map and invalidates the semantic steps when it
    differs, so a plausible value invented here would let a resumed run reuse
    judgments made by a different model without a word.
    """
    observed = observed_model_ids(environ={}, home=tmp_path)
    assert observed == {
        SESSION_MODEL_KEY: UNKNOWN_SESSION_VALUE,
        SESSION_EFFORT_KEY: UNKNOWN_SESSION_VALUE,
    }


def test_the_unknown_sentinel_is_a_value_detection_can_never_return(tmp_path: Path) -> None:
    # detect_model refuses identifiers starting with "<" as synthetic
    # placeholders, so no real model can collide with the sentinel and nobody
    # reading a manifest can mistake one for the other.
    _transcript(tmp_path, [{"message": {"role": "assistant", "model": UNKNOWN_SESSION_VALUE}}])
    assert detect_model(environ=_env(), home=tmp_path) is None


def test_an_unknown_resumes_as_unknown_and_never_as_a_named_model(tmp_path: Path) -> None:
    """What an unknown compares as on resume, decided here.

    Equal to another unknown: a host this cannot read — a Codex session, a
    machine with no transcript — still resumes, and two failed detections are
    no evidence that the model changed between them. Never equal to a named
    model: a session that became readable recomputes the semantic steps rather
    than inheriting work whose author is unrecorded.
    """
    blind = observed_model_ids(environ={}, home=tmp_path)
    assert blind == observed_model_ids(environ={"CLAUDE_CODE_SESSION_ID": "none"}, home=tmp_path)

    _transcript(tmp_path, [{"message": {"role": "assistant", "model": "claude-opus-5"}}])
    assert blind != observed_model_ids(environ=_env(), home=tmp_path)


def test_the_unknown_sentinel_cannot_be_mistaken_for_something_read() -> None:
    """Both keys, not just the model.

    `observed_model_ids` records `<unknown>` where detection failed, and resume
    treats two unknowns as equal. A detector that could return the sentinel
    would let a session claiming that literal effort resume against a run whose
    effort was never read at all.
    """
    assert detect_effort(environ={"CLAUDE_EFFORT": UNKNOWN_SESSION_VALUE}) is None
    assert detect_effort(environ={"CLAUDE_EFFORT": "<anything>"}) is None
    assert detect_effort(environ={"CLAUDE_EFFORT": "xhigh"}) == "xhigh"

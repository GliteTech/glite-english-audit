"""Direct submission client: one attempt, parsed outcomes, no retries."""

import email.message
import io
import urllib.error
import urllib.request
from types import TracebackType

import pytest

from glite_english_audit.artifacts.enums import ExampleType, Modality
from glite_english_audit.artifacts.envelope import utc_now
from glite_english_audit.artifacts.models import ModalityCounts, SafeMistakeRecord
from glite_english_audit.artifacts.submission import (
    NewSubmissionRequest,
    SubmissionAccepted,
    SubmissionCounts,
    SubmissionPackage,
    SubmissionRejected,
    compute_payload_hash,
)
from glite_english_audit.submission.client import submit_once

_SUBMISSION_ID = "sub-" + "ab" * 16
_RECOVERY_SECRET = "cd" * 32
_BASE_URL = "https://glite.example/"
_EXPECTED_URL = "https://glite.example/api/v1/submissions"


def _package() -> SubmissionPackage:
    record = SafeMistakeRecord(
        mistake="The learner wrote very like instead of really like.",
        rule="Use really, not very, before like.",
        example="I really like this plan.",
        example_type=ExampleType.SYNTHETIC,
        source_type="claude_code",
        modality=Modality.WRITTEN,
    )
    zero = ModalityCounts(
        eligible_words=0,
        analyzed_words=0,
        eligible_utterances=0,
        analyzed_utterances=0,
    )
    draft = SubmissionPackage(
        submission_schema_version=1,
        submission_id=_SUBMISSION_ID,
        recovery_secret=_RECOVERY_SECRET,
        payload_hash="0" * 64,
        client_version="0.1.0",
        producer_version="1.0.0",
        privacy_verifier_version="1.0.0",
        records=[record],
        counts=SubmissionCounts(
            eligible_english_words=100,
            analyzed_english_words=80,
            eligible_utterances=10,
            analyzed_utterances=8,
            written=zero,
            spoken_asr=zero,
            verified_total_mistakes=1,
            shared_mistakes=1,
            withheld_by_user=0,
            withheld_for_privacy=0,
        ),
    )
    return draft.model_copy(update={"payload_hash": compute_payload_hash(draft)})


def _request() -> NewSubmissionRequest:
    return NewSubmissionRequest(
        package=_package(),
        adult_attested=True,
        permanent_storage_and_uses_accepted=True,
        external_ai_processing_accepted=True,
        consent_policy_version="2026-01",
        client_confirmation_at=utc_now(),
    )


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


def _http_error(code: int, body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        _EXPECTED_URL,
        code,
        "error",
        email.message.Message(),
        io.BytesIO(body),
    )


def _install(
    monkeypatch: pytest.MonkeyPatch,
    behavior: "_FakeResponse | Exception",
) -> list[str]:
    calls: list[str] = []

    def fake_urlopen(request: urllib.request.Request, *, timeout: float) -> _FakeResponse:
        calls.append(request.full_url)
        assert request.get_method() == "POST"
        assert timeout > 0
        if isinstance(behavior, Exception):
            raise behavior
        return behavior

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return calls


def test_accepted_response_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    accepted = SubmissionAccepted(
        submission_id=_SUBMISSION_ID,
        state="received",
        report_url=None,
    )
    calls = _install(monkeypatch, _FakeResponse(accepted.model_dump_json().encode("utf-8")))
    outcome = submit_once(_BASE_URL, _request())
    assert outcome.ok
    assert outcome.accepted == accepted
    assert outcome.rejected is None
    assert outcome.transport_error is None
    assert calls == [_EXPECTED_URL]


def test_rejected_body_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    rejected = SubmissionRejected(diagnostic_codes=["SUBMISSION_HASH_MISMATCH"])
    calls = _install(monkeypatch, _http_error(422, rejected.model_dump_json().encode("utf-8")))
    outcome = submit_once(_BASE_URL, _request())
    assert not outcome.ok
    assert outcome.accepted is None
    assert outcome.rejected == rejected
    assert outcome.transport_error is None
    assert len(calls) == 1  # no retry after a rejection


def test_http_error_with_garbage_body_is_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install(monkeypatch, _http_error(500, b"<html>server exploded</html>"))
    outcome = submit_once(_BASE_URL, _request())
    assert not outcome.ok
    assert outcome.accepted is None
    assert outcome.rejected is None
    assert outcome.transport_error is not None
    assert "500" in outcome.transport_error
    assert len(calls) == 1  # no retry after a server error


def test_url_error_is_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install(monkeypatch, urllib.error.URLError("connection refused"))
    outcome = submit_once(_BASE_URL, _request())
    assert not outcome.ok
    assert outcome.accepted is None
    assert outcome.rejected is None
    assert outcome.transport_error is not None
    assert "URLError" in outcome.transport_error
    assert len(calls) == 1  # no retry after a transport failure

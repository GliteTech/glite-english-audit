"""Loopback review server: binding, token, CSRF, origin, and shutdown behavior."""

import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from email.message import Message

import pytest

from glite_english_audit.artifacts.enums import ExampleType, Modality, StageId
from glite_english_audit.artifacts.envelope import ArtifactEnvelope, utc_now
from glite_english_audit.artifacts.hashing import (
    canonical_json_bytes,
    new_artifact_id,
    new_run_id,
)
from glite_english_audit.artifacts.models import (
    AuditCounts,
    ModalityCounts,
    ReviewedRecord,
    ReviewedSubmissionArtifact,
    SafeMistakeRecord,
)
from glite_english_audit.artifacts.submission import NewSubmissionRequest
from glite_english_audit.review_server.server import (
    CSRF_HEADER,
    ReviewServerHandle,
    start_review_server,
)
from glite_english_audit.submission.capability import SubmissionCapability
from glite_english_audit.submission.client import SubmissionOutcome

_WRONG_TOKEN = "x" * 43


def _written_modality() -> ModalityCounts:
    return ModalityCounts(
        eligible_words=800,
        analyzed_words=750,
        eligible_utterances=60,
        analyzed_utterances=55,
    )


def _spoken_modality() -> ModalityCounts:
    return ModalityCounts(
        eligible_words=400,
        analyzed_words=350,
        eligible_utterances=30,
        analyzed_utterances=25,
    )


def _safe_record(*, mistake: str, rule: str, example: str) -> SafeMistakeRecord:
    return SafeMistakeRecord(
        mistake=mistake,
        rule=rule,
        example=example,
        example_type=ExampleType.SYNTHETIC,
        source_type="claude_code",
        modality=Modality.WRITTEN,
    )


def _reviewed_records() -> list[ReviewedRecord]:
    first = _safe_record(
        mistake="Wrote 'more easy' instead of 'easier'.",
        rule="Short adjectives form the comparative with -er.",
        example="This route is easier than the old one.",
    )
    second = _safe_record(
        mistake="Wrote 'informations' instead of 'information'.",
        rule="The noun 'information' is uncountable.",
        example="She gave me useful information about the city.",
    )
    return [
        ReviewedRecord(
            mistake_id="m-1",
            record=first,
            included=True,
            privacy_creator_version="0.1.0",
            privacy_verifier_version="0.1.0",
        ),
        ReviewedRecord(
            mistake_id="m-2",
            record=second,
            included=True,
            privacy_creator_version="0.1.0",
            privacy_verifier_version="0.1.0",
        ),
    ]


def _artifact() -> ReviewedSubmissionArtifact:
    records = _reviewed_records()
    counts = AuditCounts(
        eligible_english_words=1200,
        analyzed_english_words=1100,
        eligible_utterances=90,
        analyzed_utterances=80,
        written=_written_modality(),
        spoken_asr=_spoken_modality(),
        verified_total_mistakes=len(records) + 1,
        shared_mistakes=len(records),
        withheld_by_user=0,
        withheld_for_privacy=1,
    )
    envelope = ArtifactEnvelope(
        schema_name="reviewed_submission",
        schema_version=1,
        artifact_id=new_artifact_id(),
        run_id=new_run_id(),
        stage_id=StageId.REVIEWED_SUBMISSION,
        producer_name="test",
        producer_version="0.1.0",
        created_at=utc_now(),
    )
    return ReviewedSubmissionArtifact(envelope=envelope, records=records, counts=counts)


def _download_only() -> SubmissionCapability:
    return SubmissionCapability(
        direct_submission_available=False,
        reason="No Glite submission endpoint is configured. "
        "Save the package and upload it later on the Glite website.",
    )


def _direct() -> SubmissionCapability:
    return SubmissionCapability(
        direct_submission_available=True,
        reason="A compatible Glite endpoint is configured.",
        endpoint_base_url="https://glite-EXAMPLE.invalid",
    )


def _no_network_submit(url: str, request: NewSubmissionRequest) -> SubmissionOutcome:
    raise AssertionError("submit must never be called in these tests")


@contextmanager
def _running(
    *,
    capability: SubmissionCapability | None = None,
    inactivity_timeout_seconds: float = 1800.0,
    inactivity_check_seconds: float = 60.0,
) -> Iterator[ReviewServerHandle]:
    handle = start_review_server(
        _artifact(),
        capability if capability is not None else _download_only(),
        inactivity_timeout_seconds=inactivity_timeout_seconds,
        inactivity_check_seconds=inactivity_check_seconds,
        submit=_no_network_submit,
    )
    handle.serve_forever_in_thread()
    try:
        yield handle
    finally:
        handle.shutdown()


def _request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 5.0,
) -> tuple[int, bytes, Message]:
    http_request = urllib.request.Request(
        url,
        data=body,
        headers=headers or {},
        method=method,
    )
    try:
        with urllib.request.urlopen(http_request, timeout=timeout) as response:
            return response.status, response.read(), response.headers
    except urllib.error.HTTPError as error:
        return error.code, error.read(), error.headers


def _post(
    handle: ReviewServerHandle,
    route: str,
    payload: dict[str, object],
    *,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, bytes, Message]:
    headers = {"Content-Type": "application/json", CSRF_HEADER: handle.token}
    if extra_headers is not None:
        headers.update(extra_headers)
    return _request(
        handle.url + route,
        method="POST",
        headers=headers,
        body=json.dumps(payload).encode("utf-8"),
    )


def _wait_until_down(handle: ReviewServerHandle) -> None:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            _request(handle.url, timeout=1.0)
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            return
        time.sleep(0.05)
    pytest.fail("the review server did not stop")


def test_binds_only_to_loopback() -> None:
    with _running() as handle:
        assert handle.bound_host == "127.0.0.1"
        assert handle.url.startswith("http://127.0.0.1:")


def test_wrong_token_and_unknown_path_get_identical_404_bodies() -> None:
    with _running() as handle:
        base = f"http://127.0.0.1:{handle.port}"
        wrong_token_status, wrong_token_body, _ = _request(f"{base}/t/{_WRONG_TOKEN}/")
        unknown_status, unknown_body, _ = _request(f"{base}/private/artifacts")
        no_token_status, no_token_body, _ = _request(f"{base}/")
        unknown_route_status, unknown_route_body, _ = _request(handle.url + "manifest.json")
        assert wrong_token_status == 404
        assert unknown_status == 404
        assert no_token_status == 404
        assert unknown_route_status == 404
        assert wrong_token_body == unknown_body == no_token_body == unknown_route_body


def test_security_headers_present_on_every_response() -> None:
    with _running() as handle:
        for status_expected, response in (
            (200, _request(handle.url)),
            (404, _request(f"http://127.0.0.1:{handle.port}/t/{_WRONG_TOKEN}/")),
        ):
            status, _, headers = response
            assert status == status_expected
            csp = headers.get("Content-Security-Policy")
            assert csp is not None and "default-src 'none'" in csp
            assert headers.get("Cache-Control") == "no-store"
            assert headers.get("X-Content-Type-Options") == "nosniff"
            assert headers.get("Referrer-Policy") == "no-referrer"


def test_page_over_http_contains_records_and_unchecked_confirmations() -> None:
    with _running() as handle:
        status, body, headers = _request(handle.url)
        assert status == 200
        content_type = headers.get("Content-Type")
        assert content_type is not None and content_type.startswith("text/html")
        page = body.decode("utf-8")
        assert "Wrote &#x27;more easy&#x27; instead of &#x27;easier&#x27;." in page
        assert "Wrote &#x27;informations&#x27; instead of &#x27;information&#x27;." in page
        adult = re.search(r'<input[^>]*id="adult-confirmed"[^>]*>', page)
        storage = re.search(r'<input[^>]*id="storage-confirmed"[^>]*>', page)
        assert adult is not None and " checked" not in adult.group(0)
        assert storage is not None and " checked" not in storage.group(0)


def test_post_without_csrf_header_is_403() -> None:
    with _running() as handle:
        status, body, _ = _request(
            handle.url + "decisions",
            method="POST",
            headers={"Content-Type": "application/json"},
            body=json.dumps({"mistake_id": "m-1", "included": False}).encode("utf-8"),
        )
        assert status == 403
        assert b"X-Glite-Review" in body
        assert handle.state.included_count == 2


def test_post_with_wrong_csrf_header_is_403() -> None:
    with _running() as handle:
        status, _, _ = _post(
            handle,
            "decisions",
            {"mistake_id": "m-1", "included": False},
            extra_headers={CSRF_HEADER: _WRONG_TOKEN},
        )
        assert status == 403
        assert handle.state.included_count == 2


def test_foreign_origin_is_rejected_even_with_valid_token() -> None:
    with _running() as handle:
        get_status, _, _ = _request(
            handle.url,
            headers={"Origin": "http://evil.example"},
        )
        assert get_status == 403
        post_status, _, _ = _post(
            handle,
            "decisions",
            {"mistake_id": "m-1", "included": False},
            extra_headers={"Origin": "https://evil.example"},
        )
        assert post_status == 403
        assert handle.state.included_count == 2


def test_own_origin_is_accepted() -> None:
    with _running() as handle:
        status, _, _ = _request(
            handle.url,
            headers={"Origin": f"http://127.0.0.1:{handle.port}"},
        )
        assert status == 200


def test_package_json_matches_state_canonical_bytes() -> None:
    with _running() as handle:
        status, body, headers = _request(handle.url + "package.json")
        assert status == 200
        package = handle.state.current_package()
        assert package is not None
        assert body == canonical_json_bytes(package.model_dump(mode="json"))
        disposition = headers.get("Content-Disposition")
        assert disposition is not None and disposition.startswith("attachment")


def test_toggle_updates_counts_and_changes_package_payload_id() -> None:
    with _running() as handle:
        _, first_body, _ = _request(handle.url + "package.json")
        first = json.loads(first_body)
        assert len(first["records"]) == 2
        assert first["counts"]["withheld_by_user"] == 0

        status, decision_body, _ = _post(
            handle, "decisions", {"mistake_id": "m-1", "included": False}
        )
        assert status == 200
        decision = json.loads(decision_body)
        assert decision["will_send"] == 1
        assert decision["withheld_by_user"] == 1
        assert decision["counts"]["shared_mistakes"] == 1
        assert decision["counts"]["verified_total_mistakes"] == 3

        _, second_body, _ = _request(handle.url + "package.json")
        second = json.loads(second_body)
        assert len(second["records"]) == 1
        assert second["counts"]["withheld_by_user"] == 1
        assert second["submission_id"] != first["submission_id"]
        assert second["payload_hash"] != first["payload_hash"]
        assert second["recovery_secret"] != first["recovery_secret"]


def test_excluded_record_details_absent_from_package() -> None:
    with _running() as handle:
        _post(handle, "decisions", {"mistake_id": "m-2", "included": False})
        _, body, _ = _request(handle.url + "package.json")
        assert b"informations" not in body
        assert json.loads(body)["counts"]["withheld_by_user"] == 1


def test_package_json_with_zero_included_is_409() -> None:
    with _running() as handle:
        _post(handle, "decisions", {"mistake_id": "m-1", "included": False})
        _post(handle, "decisions", {"mistake_id": "m-2", "included": False})
        status, body, _ = _request(handle.url + "package.json")
        assert status == 409
        assert b"no useful data" in body


def test_confirmation_decisions_round_trip() -> None:
    with _running() as handle:
        assert handle.state.adult_confirmed is False
        assert handle.state.storage_confirmed is False
        _, body, _ = _post(handle, "decisions", {"adult_confirmed": True})
        assert json.loads(body)["adult_confirmed"] is True
        _, body, _ = _post(handle, "decisions", {"storage_confirmed": True})
        assert json.loads(body)["storage_confirmed"] is True
        assert handle.state.adult_confirmed is True
        assert handle.state.storage_confirmed is True


def test_unknown_mistake_id_is_400() -> None:
    with _running() as handle:
        status, _, _ = _post(handle, "decisions", {"mistake_id": "m-999", "included": False})
        assert status == 400


def test_submit_without_confirmations_is_409() -> None:
    with _running(capability=_direct()) as handle:
        status, body, _ = _post(handle, "submit", {})
        assert status == 409
        assert b"confirmations" in body


def test_submit_in_download_only_mode_is_409() -> None:
    with _running() as handle:
        handle.state.set_adult_confirmed(True)
        handle.state.set_storage_confirmed(True)
        status, body, _ = _post(handle, "submit", {})
        assert status == 409
        assert b"Direct sending is not available" in body


def test_submit_with_zero_included_is_409_no_useful_data() -> None:
    with _running(capability=_direct()) as handle:
        _post(handle, "decisions", {"adult_confirmed": True})
        _post(handle, "decisions", {"storage_confirmed": True})
        _post(handle, "decisions", {"mistake_id": "m-1", "included": False})
        _post(handle, "decisions", {"mistake_id": "m-2", "included": False})
        status, body, _ = _post(handle, "submit", {})
        assert status == 409
        assert b"no useful data" in body


def test_shutdown_route_stops_the_server() -> None:
    with _running() as handle:
        status, _, _ = _post(handle, "shutdown", {})
        assert status == 200
        _wait_until_down(handle)


def test_inactivity_timeout_stops_the_server() -> None:
    with _running(
        inactivity_timeout_seconds=0.05,
        inactivity_check_seconds=0.02,
    ) as handle:
        status, _, _ = _request(handle.url)
        assert status == 200
        _wait_until_down(handle)


def test_shutdown_is_idempotent() -> None:
    with _running() as handle:
        handle.shutdown()
        handle.shutdown()

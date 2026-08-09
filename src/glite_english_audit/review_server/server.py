"""Loopback-only HTTP server for the final local review page.

Security model (specification, 2.6, 13.6):

- The socket binds only to 127.0.0.1; no other interface is ever offered.
- Every route lives under an unguessable per-run token path prefix. A wrong or
  missing token returns 404 with a body identical to any unknown path, so the
  token cannot be probed apart from ordinary path guessing.
- Every POST additionally requires the token in an ``X-Glite-Review`` header,
  which a cross-site form or fetch cannot supply; a present ``Origin`` header
  must name this server exactly.
- Only the review routes exist. No other private artifact is reachable.
- The server shuts itself down after 30 minutes without a request.
"""

import contextlib
import hmac
import json
import secrets
import threading
import time
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

from glite_english_audit.artifacts.envelope import utc_now
from glite_english_audit.artifacts.models import ReviewedSubmissionArtifact
from glite_english_audit.artifacts.submission import NewSubmissionRequest
from glite_english_audit.pipeline.record_consent import record_consent
from glite_english_audit.review_server.page import CONSENT_POLICY_VERSION, render_page
from glite_english_audit.review_server.session import ReviewSessionState, UnknownMistakeError
from glite_english_audit.submission.capability import SubmissionCapability
from glite_english_audit.submission.client import SubmissionOutcome, submit_once

CSRF_HEADER = "X-Glite-Review"
INACTIVITY_TIMEOUT_SECONDS = 30.0 * 60.0
INACTIVITY_CHECK_SECONDS = 60.0

_LOOPBACK_HOST = "127.0.0.1"
_MAX_BODY_BYTES = 64 * 1024
_NOT_FOUND_BODY = b"Not found.\n"
_SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    (
        "Content-Security-Policy",
        "default-src 'none'; style-src 'unsafe-inline'; "
        "script-src 'unsafe-inline'; connect-src 'self'",
    ),
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
)

_SubmitFn = Callable[[str, NewSubmissionRequest], SubmissionOutcome]


def _token_equal(candidate: str, token: str) -> bool:
    """Constant-time token comparison over bytes."""
    return hmac.compare_digest(
        candidate.encode("latin-1", errors="replace"),
        token.encode("ascii"),
    )


class _ReviewHTTPServer(ThreadingHTTPServer):
    """Loopback HTTP server carrying the shared review session."""

    daemon_threads = True

    def __init__(
        self,
        port: int,
        *,
        state: ReviewSessionState,
        capability: SubmissionCapability,
        token: str,
        monotonic: Callable[[], float],
        submit: _SubmitFn,
        run_id: str | None = None,
        runs_root: Path | None = None,
    ) -> None:
        super().__init__((_LOOPBACK_HOST, port), _ReviewRequestHandler)
        self.state = state
        self.capability = capability
        self.token = token
        self.submit = submit
        # Where the two send confirmations are recorded. Absent in tests that
        # exercise the page without a run behind it; recording is skipped then
        # rather than invented.
        self.run_id = run_id
        self.runs_root = runs_root
        self.monitor: _InactivityMonitor | None = None
        self.serving = False
        self._monotonic = monotonic
        self._activity_lock = threading.Lock()
        self._last_activity = monotonic()
        self._stop_lock = threading.Lock()
        self._stopped = False

    @property
    def stopped(self) -> bool:
        with self._stop_lock:
            return self._stopped

    def touch(self) -> None:
        with self._activity_lock:
            self._last_activity = self._monotonic()

    def idle_seconds(self) -> float:
        with self._activity_lock:
            return self._monotonic() - self._last_activity

    def stop_completely(self) -> None:
        """Stop serving, close the socket, and stop the inactivity monitor."""
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
        if self.monitor is not None:
            self.monitor.stop()
        if self.serving:
            self.shutdown()
        self.server_close()


class _InactivityMonitor(threading.Thread):
    """Daemon timer that stops the server after a quiet period."""

    def __init__(
        self,
        server: _ReviewHTTPServer,
        *,
        timeout_seconds: float,
        check_seconds: float,
    ) -> None:
        super().__init__(name="glite-review-inactivity", daemon=True)
        self._server = server
        self._timeout_seconds = timeout_seconds
        self._check_seconds = check_seconds
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.wait(self._check_seconds):
            if self._server.idle_seconds() >= self._timeout_seconds:
                self._server.stop_completely()
                return

    def stop(self) -> None:
        self._stop_event.set()


class _ReviewRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def _review(self) -> _ReviewHTTPServer:
        return cast(_ReviewHTTPServer, self.server)

    def version_string(self) -> str:
        return "GliteReview"

    def log_message(self, format: str, *args: object) -> None:
        """Never log requests: the URL path contains the capability token."""

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        try:
            self._route_request(method)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            with contextlib.suppress(OSError):
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"reason": "internal error"},
                )

    def _route_request(self, method: str) -> None:
        review = self._review
        review.touch()
        origin = self.headers.get("Origin")
        if origin is not None and origin != f"http://{_LOOPBACK_HOST}:{review.server_port}":
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {"reason": "cross-origin requests are not allowed"},
            )
            return
        route = self._match_token_route()
        if route is None:
            self._send_not_found()
            return
        if method == "GET":
            self._route_get(route)
            return
        header_token = self.headers.get(CSRF_HEADER)
        if header_token is None or not _token_equal(header_token, review.token):
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {"reason": f"missing or wrong {CSRF_HEADER} header"},
            )
            return
        self._route_post(route)

    def _match_token_route(self) -> str | None:
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        segments = path.split("/")
        if len(segments) < 4 or segments[0] or segments[1] != "t":
            return None
        if not _token_equal(segments[2], self._review.token):
            return None
        return "/" + "/".join(segments[3:])

    def _route_get(self, route: str) -> None:
        if route == "/":
            self._get_page()
        elif route == "/package.json":
            self._get_package()
        else:
            self._send_not_found()

    def _route_post(self, route: str) -> None:
        if route == "/decisions":
            self._post_decisions()
        elif route == "/submit":
            self._post_submit()
        elif route == "/shutdown":
            self._post_shutdown()
        else:
            self._send_not_found()

    def _get_page(self) -> None:
        review = self._review
        document = render_page(review.state, review.capability, review.token)
        self._send(
            HTTPStatus.OK,
            document.encode("utf-8"),
            "text/html; charset=utf-8",
        )

    def _get_package(self) -> None:
        data = self._review.state.current_package_bytes()
        if data is None:
            self._send_json(
                HTTPStatus.CONFLICT,
                {"reason": "Every record is excluded, so there is no useful data to send."},
            )
            return
        self._send(
            HTTPStatus.OK,
            data,
            "application/json; charset=utf-8",
            extra_headers=(
                ("Content-Disposition", 'attachment; filename="glite-submission-package.json"'),
            ),
        )

    def _read_json_body(self) -> dict[str, object] | None:
        length_header = self.headers.get("Content-Length")
        try:
            length = int(length_header) if length_header is not None else 0
        except ValueError:
            return None
        if length < 0 or length > _MAX_BODY_BYTES:
            return None
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        return cast(dict[str, object], payload)

    def _post_decisions(self) -> None:
        payload = self._read_json_body()
        if payload is None:
            self._send_json(HTTPStatus.BAD_REQUEST, {"reason": "the request body is not valid"})
            return
        state = self._review.state
        if "mistake_id" in payload:
            mistake_id = payload.get("mistake_id")
            included = payload.get("included")
            if not isinstance(mistake_id, str) or not isinstance(included, bool):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"reason": "a decision needs a mistake_id and an included flag"},
                )
                return
            try:
                state.set_included(mistake_id, included)
            except UnknownMistakeError:
                self._send_json(HTTPStatus.BAD_REQUEST, {"reason": "unknown mistake_id"})
                return
        elif isinstance(payload.get("adult_confirmed"), bool):
            state.set_adult_confirmed(cast(bool, payload["adult_confirmed"]))
            self._record_consent_if_checked("adult", checked=state.adult_confirmed)
        elif isinstance(payload.get("storage_confirmed"), bool):
            state.set_storage_confirmed(cast(bool, payload["storage_confirmed"]))
            self._record_consent_if_checked("storage-terms", checked=state.storage_confirmed)
        else:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"reason": "the request body is not a known decision"},
            )
            return
        self._send_json(HTTPStatus.OK, self._counts_payload())

    def _record_consent_if_checked(self, moment: str, *, checked: bool) -> None:
        """Write a ticked confirmation into the run manifest.

        Specification 2.2 counts these as consent moments, and until now they
        lived only in this process's memory: the page held them, the server
        wrote nothing, and a finished run's manifest said the user had never
        agreed to anything about sending. A consent nobody recorded cannot be
        told apart afterwards from one nobody asked for.

        Only ticking is recorded, never unticking. The record says a person
        agreed at a moment, which stays true whether or not they later change
        their mind; what a later untick changes is whether sending is allowed,
        and that is read live from this session, not from the manifest.

        A manifest that cannot be written must not stop the review. The user
        is mid-decision on their own machine, and the send path checks the
        live session rather than this record, so a failed write costs an audit
        trail entry and nothing else.
        """
        review = self._review
        if not checked or review.run_id is None:
            return
        try:
            record_consent(review.run_id, moment, runs_root=review.runs_root)
        except (OSError, ValueError):
            return

    def _counts_payload(self) -> dict[str, object]:
        state = self._review.state
        counts = state.counts
        return {
            "will_send": state.included_count,
            "withheld_by_user": counts.withheld_by_user,
            "shared_mistakes": counts.shared_mistakes,
            "verified_total_mistakes": counts.verified_total_mistakes,
            "adult_confirmed": state.adult_confirmed,
            "storage_confirmed": state.storage_confirmed,
            "counts": counts.model_dump(mode="json"),
        }

    def _post_submit(self) -> None:
        self._read_json_body()  # drain the request body before responding
        review = self._review
        capability = review.capability
        state = review.state
        if not capability.direct_submission_available or capability.endpoint_base_url is None:
            self._send_json(
                HTTPStatus.CONFLICT,
                {
                    "reason": "Direct sending is not available. Save the package "
                    "and upload it later on the Glite website."
                },
            )
            return
        if not (state.adult_confirmed and state.storage_confirmed):
            self._send_json(
                HTTPStatus.CONFLICT,
                {"reason": "Check both required confirmations before sending."},
            )
            return
        if state.included_count == 0:
            self._send_json(
                HTTPStatus.CONFLICT,
                {
                    "reason": "Every record is excluded, so there is no useful "
                    "data to report. Nothing was sent."
                },
            )
            return
        package = state.current_package()
        if package is None:
            self._send_json(
                HTTPStatus.CONFLICT,
                {"reason": "The package failed its final checks, so nothing was sent."},
            )
            return
        request = NewSubmissionRequest(
            package=package,
            adult_attested=True,
            permanent_storage_and_uses_accepted=True,
            external_ai_processing_accepted=True,
            consent_policy_version=CONSENT_POLICY_VERSION,
            client_confirmation_at=utc_now(),
        )
        outcome = review.submit(capability.endpoint_base_url, request)
        if outcome.accepted is not None:
            self._send_json(
                HTTPStatus.OK,
                {
                    "status": outcome.accepted.state,
                    "submission_id": outcome.accepted.submission_id,
                    "report_url": outcome.accepted.report_url,
                },
            )
        elif outcome.rejected is not None:
            self._send_json(
                HTTPStatus.BAD_GATEWAY,
                {
                    "reason": "The Glite endpoint rejected the package.",
                    "diagnostic_codes": outcome.rejected.diagnostic_codes,
                },
            )
        else:
            self._send_json(
                HTTPStatus.BAD_GATEWAY,
                {
                    # The page prints this after "Not sent. ", so it starts a
                    # sentence and is capitalized like one.
                    "reason": (outcome.transport_error or "The request could not be completed")
                    + ". Nothing was retried."
                },
            )

    def _post_shutdown(self) -> None:
        self._read_json_body()  # drain the request body before responding
        self._send_json(HTTPStatus.OK, {"status": "shutting down"})
        threading.Thread(
            target=self._review.stop_completely,
            name="glite-review-shutdown",
            daemon=True,
        ).start()

    def _send_json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _send_not_found(self) -> None:
        self._send(HTTPStatus.NOT_FOUND, _NOT_FOUND_BODY, "text/plain; charset=utf-8")

    def _send(
        self,
        status: HTTPStatus,
        body: bytes,
        content_type: str,
        *,
        extra_headers: tuple[tuple[str, str], ...] = (),
    ) -> None:
        self.send_response(int(status))
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in _SECURITY_HEADERS:
            self.send_header(name, value)
        for name, value in extra_headers:
            self.send_header(name, value)
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)


class ReviewServerHandle:
    """Control handle for one running review server."""

    def __init__(self, server: _ReviewHTTPServer) -> None:
        self._server = server
        self._thread: threading.Thread | None = None
        self._thread_lock = threading.Lock()

    @property
    def token(self) -> str:
        return self._server.token

    @property
    def port(self) -> int:
        return self._server.server_port

    @property
    def url(self) -> str:
        return f"http://{_LOOPBACK_HOST}:{self.port}/t/{self.token}/"

    @property
    def bound_host(self) -> str:
        """The address the listening socket is actually bound to."""
        return str(self._server.socket.getsockname()[0])

    @property
    def state(self) -> ReviewSessionState:
        return self._server.state

    def serve_forever_in_thread(self) -> threading.Thread:
        """Start serving on a daemon thread and return that thread."""
        with self._thread_lock:
            if self._thread is not None:
                return self._thread
            if self._server.stopped:
                msg = "the review server is already shut down"
                raise RuntimeError(msg)
            self._server.serving = True
            thread = threading.Thread(
                target=self._server.serve_forever,
                kwargs={"poll_interval": 0.05},
                name="glite-review-server",
                daemon=True,
            )
            thread.start()
            self._thread = thread
            return thread

    def shutdown(self) -> None:
        """Stop the server and release the socket. Safe to call twice."""
        self._server.stop_completely()
        with self._thread_lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout=5.0)


def start_review_server(
    reviewed: ReviewedSubmissionArtifact,
    capability: SubmissionCapability,
    *,
    port: int = 0,
    inactivity_timeout_seconds: float = INACTIVITY_TIMEOUT_SECONDS,
    inactivity_check_seconds: float = INACTIVITY_CHECK_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    submit: _SubmitFn = submit_once,
    run_id: str | None = None,
    runs_root: Path | None = None,
) -> ReviewServerHandle:
    """Bind the loopback review server and return its control handle.

    The server is bound but not yet serving; call
    ``handle.serve_forever_in_thread()`` to start it. The timing parameters
    exist for tests; production callers keep the defaults.
    """
    token = secrets.token_urlsafe(32)
    server = _ReviewHTTPServer(
        port,
        state=ReviewSessionState(reviewed),
        capability=capability,
        token=token,
        monotonic=monotonic,
        submit=submit,
        run_id=run_id,
        runs_root=runs_root,
    )
    monitor = _InactivityMonitor(
        server,
        timeout_seconds=inactivity_timeout_seconds,
        check_seconds=inactivity_check_seconds,
    )
    server.monitor = monitor
    monitor.start()
    return ReviewServerHandle(server)

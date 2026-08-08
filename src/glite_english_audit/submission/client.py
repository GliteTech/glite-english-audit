"""Direct submission client.

One attempt per explicit user action: a failed request is reported and never
retried in the background (specification, 2.7). Idempotency lives in the
package's submission ID, so an accidental duplicate send cannot create a
second stored submission.
"""

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from glite_english_audit.artifacts.submission import (
    NewSubmissionRequest,
    SubmissionAccepted,
    SubmissionRejected,
)

_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class SubmissionOutcome:
    """Result of exactly one submission attempt."""

    accepted: SubmissionAccepted | None
    rejected: SubmissionRejected | None
    transport_error: str | None

    @property
    def ok(self) -> bool:
        return self.accepted is not None


def submit_once(endpoint_base_url: str, request: NewSubmissionRequest) -> SubmissionOutcome:
    """Send one new-submission request. Never retries."""
    url = endpoint_base_url.rstrip("/") + "/api/v1/submissions"
    body = json.dumps(request.model_dump(mode="json")).encode("utf-8")
    http_request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(http_request, timeout=_TIMEOUT_SECONDS) as response:
            payload = response.read().decode("utf-8")
        return SubmissionOutcome(
            accepted=SubmissionAccepted.model_validate_json(payload),
            rejected=None,
            transport_error=None,
        )
    except urllib.error.HTTPError as error:
        try:
            rejected = SubmissionRejected.model_validate_json(error.read().decode("utf-8"))
        except ValueError:
            return SubmissionOutcome(
                accepted=None,
                rejected=None,
                transport_error=f"the server returned HTTP {error.code}",
            )
        return SubmissionOutcome(accepted=None, rejected=rejected, transport_error=None)
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        return SubmissionOutcome(
            accepted=None,
            rejected=None,
            transport_error=f"the request could not be completed: {error.__class__.__name__}",
        )

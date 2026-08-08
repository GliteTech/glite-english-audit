"""Server-rendered HTML for the final local review page.

One self-contained document: a single inline style block, a small inline
script, and no request to anything except the loopback server itself. Light
and dark themes are both first-class through CSS custom properties and
``prefers-color-scheme``, so the first paint is correct without a theme flash
(specification, 12.3, 12.4).
"""

import html

from glite_english_audit.artifacts.models import ReviewedRecord
from glite_english_audit.consent import CONSENT_POLICY_VERSION
from glite_english_audit.review_server.session import ReviewSessionState
from glite_english_audit.submission.capability import SubmissionCapability

__all__ = ["CONSENT_POLICY_VERSION"]

ADULT_CONFIRMATION_TEXT = "I confirm that I am at least 18 years old."
STORAGE_CONFIRMATION_TEXT = (
    "I understand and accept permanent, irrevocable storage of my submitted "
    "records; the disclosed product, knowledge-graph, model-training, and "
    "aggregate-research uses; and external AI processing of privacy-safe "
    "submitted records for report and flashcard generation. Glite cannot "
    "delete these records later."
)
DOWNLOAD_ONLY_TEXT = (
    "Direct sending is not available in this run. Save the package now and "
    "upload it later on the Glite website."
)
EXCLUSION_EXPLANATION_TEXT = (
    "Excluding a record removes its details from the submission. The record "
    "still counts as one withheld mistake in the anonymous counts."
)

_STYLE = """
:root {
  color-scheme: light dark;
  --bg: #FBFCFF;
  --ink: #020306;
  --ink-soft: #1E1F22;
  --line: rgba(2, 3, 6, 0.18);
  --well: rgba(2, 3, 6, 0.05);
  --action: #005BFF;
  --action-text: #005BFF;
  --ok: #0F7B3F;
  --fail: #C21F3A;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #020306;
    --ink: #FBFCFF;
    --ink-soft: rgba(251, 252, 255, 0.72);
    --line: rgba(251, 252, 255, 0.2);
    --well: #1E1F22;
    --action-text: #7DA6FF;
    --ok: #4CC583;
    --fail: #FF7A8C;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0 auto;
  padding: 1.5rem 1.25rem 4rem;
  max-width: 46rem;
  background: var(--bg);
  color: var(--ink);
  font: 16px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif;
}
h1 { font-size: 1.5rem; line-height: 1.3; margin: 0 0 0.5rem; }
h2 { font-size: 1.125rem; margin: 2rem 0 0.5rem; }
p { margin: 0.5rem 0; }
.muted { color: var(--ink-soft); }
dl.summary {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 0.25rem 1rem;
  margin: 0.75rem 0;
}
dl.summary dt { color: var(--ink-soft); }
dl.summary dd { margin: 0; text-align: right; font-variant-numeric: tabular-nums; }
ul.records { list-style: none; margin: 1rem 0; padding: 0; border-top: 1px solid var(--line); }
li.record { border-bottom: 1px solid var(--line); padding: 0.85rem 0; }
.record-include {
  display: flex;
  gap: 0.5rem;
  align-items: center;
  font-weight: 600;
  margin: 0 0 0.5rem;
}
dl.record-fields {
  display: grid;
  grid-template-columns: 7.5rem 1fr;
  gap: 0.2rem 1rem;
  margin: 0;
}
dl.record-fields dt { color: var(--ink-soft); font-size: 0.875rem; padding-top: 0.1rem; }
dl.record-fields dd { margin: 0; }
@media (max-width: 480px) {
  dl.record-fields { grid-template-columns: 1fr; }
  dl.record-fields dd { margin-bottom: 0.35rem; }
}
input[type="checkbox"] {
  width: 1.1rem;
  height: 1.1rem;
  accent-color: var(--action);
  flex-shrink: 0;
}
.will-send { font-weight: 600; }
pre {
  background: var(--well);
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 0.75rem;
  overflow-x: auto;
  font-size: 0.8125rem;
  line-height: 1.45;
}
fieldset.confirmations {
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 0.5rem 1rem 0.75rem;
  margin: 1rem 0;
}
legend { font-weight: 600; padding: 0 0.35rem; }
.confirm-row { display: flex; gap: 0.6rem; align-items: flex-start; margin: 0.6rem 0; }
.confirm-row input { margin-top: 0.2rem; }
.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 0.75rem;
  align-items: center;
  margin: 1.25rem 0 0.5rem;
}
.button {
  display: inline-block;
  border-radius: 6px;
  padding: 0.55rem 1.1rem;
  font: inherit;
  font-weight: 600;
  text-decoration: none;
  cursor: pointer;
}
.button.primary { background: var(--action); color: #FBFCFF; border: 1px solid var(--action); }
.button.primary[disabled] { opacity: 0.45; cursor: not-allowed; }
.button.secondary {
  background: transparent;
  color: var(--action-text);
  border: 1px solid var(--action-text);
}
a.button.secondary:visited { color: var(--action-text); }
#submit-status:empty { display: none; }
.status-ok { color: var(--ok); font-weight: 600; }
.status-fail { color: var(--fail); font-weight: 600; }
:focus-visible { outline: 3px solid var(--action-text); outline-offset: 2px; }
"""

_SCRIPT = """
(function () {
  "use strict";
  var token = document.body.getAttribute("data-token") || "";
  var sendButton = document.getElementById("send-button");
  var status = document.getElementById("submit-status");

  function applyCounts(data) {
    var willSend = document.getElementById("will-send-count");
    if (willSend) { willSend.textContent = String(data.will_send); }
    var sendCount = document.getElementById("send-count");
    if (sendCount) { sendCount.textContent = String(data.will_send); }
    var withheld = document.getElementById("withheld-user-count");
    if (withheld) { withheld.textContent = String(data.withheld_by_user); }
    if (sendButton) {
      sendButton.disabled = !(
        data.adult_confirmed && data.storage_confirmed && data.will_send > 0
      );
    }
  }

  function postDecision(payload) {
    return fetch("decisions", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Glite-Review": token },
      body: JSON.stringify(payload)
    }).then(function (response) { return response.json(); }).then(applyCounts);
  }

  function refreshPackage() {
    var view = document.querySelector("#package-view code");
    var empty = document.getElementById("package-empty");
    fetch("package.json").then(function (response) {
      if (!response.ok) { throw new Error("no package"); }
      return response.text();
    }).then(function (text) {
      if (view) { view.textContent = text; view.parentElement.hidden = false; }
      if (empty) { empty.hidden = true; }
    }).catch(function () {
      if (view) { view.textContent = ""; view.parentElement.hidden = true; }
      if (empty) { empty.hidden = false; }
    });
  }

  Array.prototype.forEach.call(
    document.querySelectorAll(".record-toggle"),
    function (box) {
      box.addEventListener("change", function () {
        postDecision({
          mistake_id: box.getAttribute("data-mistake-id"),
          included: box.checked
        }).then(refreshPackage);
      });
    }
  );

  Array.prototype.forEach.call(
    document.querySelectorAll(".confirm-toggle"),
    function (box) {
      box.addEventListener("change", function () {
        var payload = {};
        payload[box.getAttribute("data-confirm")] = box.checked;
        postDecision(payload);
      });
    }
  );

  if (sendButton) {
    sendButton.addEventListener("click", function () {
      sendButton.disabled = true;
      if (status) { status.className = ""; status.textContent = "Sending."; }
      fetch("submit", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Glite-Review": token },
        body: "{}"
      }).then(function (response) {
        return response.json().then(function (data) {
          return { ok: response.ok, data: data };
        });
      }).then(function (result) {
        if (!status) { return; }
        if (result.ok) {
          status.className = "status-ok";
          status.textContent = "Sent. Submission ID: " + result.data.submission_id;
        } else {
          status.className = "status-fail";
          status.textContent = result.data.reason || "Sending failed.";
          sendButton.disabled = false;
        }
      }).catch(function () {
        if (status) {
          status.className = "status-fail";
          status.textContent = "The request failed. Nothing was retried.";
        }
        sendButton.disabled = false;
      });
    });
  }
})();
"""


def _escape(value: str) -> str:
    return html.escape(value, quote=True)


def _record_row(index: int, entry: ReviewedRecord) -> str:
    record = entry.record
    checkbox_id = f"include-{index}"
    checked = " checked" if entry.included else ""
    return (
        '<li class="record">'
        '<p class="record-include">'
        f'<input type="checkbox" id="{checkbox_id}" class="record-toggle" '
        f'data-mistake-id="{_escape(entry.mistake_id)}"{checked}> '
        f'<label for="{checkbox_id}">Include this mistake in the submission</label>'
        "</p>"
        '<dl class="record-fields">'
        f"<dt>Mistake</dt><dd>{_escape(record.mistake)}</dd>"
        f"<dt>Rule</dt><dd>{_escape(record.rule)}</dd>"
        f"<dt>Example</dt><dd>{_escape(record.example)}</dd>"
        f"<dt>Example type</dt><dd>{_escape(record.example_type.value)}</dd>"
        f"<dt>Source</dt><dd>{_escape(record.source_type)}</dd>"
        f"<dt>Modality</dt><dd>{_escape(record.modality.value)}</dd>"
        "</dl>"
        "</li>"
    )


def _summary_section(state: ReviewSessionState) -> str:
    counts = state.counts
    return (
        '<section aria-labelledby="summary-heading">'
        '<h2 id="summary-heading">What this audit covered</h2>'
        '<dl class="summary">'
        "<dt>Eligible English words analyzed</dt>"
        f"<dd>{counts.analyzed_english_words} of {counts.eligible_english_words}</dd>"
        "<dt>Eligible utterances analyzed</dt>"
        f"<dd>{counts.analyzed_utterances} of {counts.eligible_utterances}</dd>"
        f"<dt>Verified mistakes</dt><dd>{counts.verified_total_mistakes}</dd>"
        "<dt>Could not be made safe to share</dt>"
        f"<dd>{counts.withheld_for_privacy}</dd>"
        "<dt>Withheld by you</dt>"
        f'<dd id="withheld-user-count">{counts.withheld_by_user}</dd>'
        "</dl>"
        "</section>"
    )


def _records_section(state: ReviewSessionState) -> str:
    records = state.records
    rows = "".join(_record_row(index, entry) for index, entry in enumerate(records))
    return (
        '<section aria-labelledby="records-heading">'
        f'<h2 id="records-heading">Mistake records ({len(records)})</h2>'
        "<p>Each record below is shown exactly as it would be sent. "
        "You can include or exclude records; you cannot edit them.</p>"
        f"<p>{_escape(EXCLUSION_EXPLANATION_TEXT)}</p>"
        f'<ul class="records">{rows}</ul>'
        '<p class="will-send" aria-live="polite">Will send '
        f'<span id="will-send-count">{state.included_count}</span> mistakes.</p>'
        "</section>"
    )


def _package_section(state: ReviewSessionState) -> str:
    package_bytes = state.current_package_bytes()
    if package_bytes is None:
        body = ""
        pre_hidden = " hidden"
        empty_hidden = ""
    else:
        body = _escape(package_bytes.decode("utf-8"))
        pre_hidden = ""
        empty_hidden = " hidden"
    return (
        '<section aria-labelledby="package-heading">'
        '<h2 id="package-heading">Exact submission package</h2>'
        "<p>This JSON is byte for byte what Glite would receive.</p>"
        f'<pre id="package-view" tabindex="0"{pre_hidden}><code>{body}</code></pre>'
        f'<p id="package-empty" class="muted"{empty_hidden}>'
        "Every record is excluded, so there is no package to send.</p>"
        "</section>"
    )


def _confirmations(state: ReviewSessionState) -> str:
    adult_checked = " checked" if state.adult_confirmed else ""
    storage_checked = " checked" if state.storage_confirmed else ""
    return (
        '<fieldset class="confirmations">'
        "<legend>Required confirmations</legend>"
        '<p class="confirm-row">'
        '<input type="checkbox" id="adult-confirmed" class="confirm-toggle" '
        f'data-confirm="adult_confirmed"{adult_checked}> '
        f'<label for="adult-confirmed">{_escape(ADULT_CONFIRMATION_TEXT)}</label>'
        "</p>"
        '<p class="confirm-row">'
        '<input type="checkbox" id="storage-confirmed" class="confirm-toggle" '
        f'data-confirm="storage_confirmed"{storage_checked}> '
        f'<label for="storage-confirmed">{_escape(STORAGE_CONFIRMATION_TEXT)}</label>'
        "</p>"
        "</fieldset>"
    )


def _actions(state: ReviewSessionState, capability: SubmissionCapability) -> str:
    download = (
        '<a class="button secondary" href="package.json" '
        'download="glite-submission-package.json">Download the package</a>'
    )
    if capability.direct_submission_available:
        action = (
            '<button type="button" class="button primary" id="send-button" disabled>'
            f'Send <span id="send-count">{state.included_count}</span> '
            "mistakes anonymously</button>"
        )
        note = ""
    else:
        action = ""
        note = f'<p id="download-only-note">{_escape(DOWNLOAD_ONLY_TEXT)}</p>'
    return (
        '<section aria-labelledby="send-heading">'
        '<h2 id="send-heading">Send or save</h2>'
        + _confirmations(state)
        + f'<p class="actions">{download}{action}</p>'
        + note
        + '<p id="submit-status" role="status"></p>'
        "</section>"
    )


def render_page(
    state: ReviewSessionState,
    capability: SubmissionCapability,
    token: str,
) -> str:
    """Render the complete review page for the current session state."""
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="color-scheme" content="light dark">\n'
        '<meta name="referrer" content="no-referrer">\n'
        "<title>Glite English audit review</title>\n"
        f"<style>{_STYLE}</style>\n"
        "</head>\n"
        f'<body data-token="{_escape(token)}">\n'
        "<main>\n"
        "<h1>Final review before anything leaves this computer</h1>\n"
        "<p>Nothing has been sent yet. Review each record below. "
        "Only what you see on this page can go to Glite.</p>\n"
        + _summary_section(state)
        + _records_section(state)
        + _package_section(state)
        + _actions(state, capability)
        + "\n</main>\n"
        f"<script>{_SCRIPT}</script>\n"
        "</body>\n"
        "</html>\n"
    )

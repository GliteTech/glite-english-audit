"""Server-rendered HTML for the final local review page.

One self-contained document: a single inline style block, a small inline
script, and no request to anything except the loopback server itself. Light
and dark themes are both first-class through CSS custom properties and
``prefers-color-scheme``, so the first paint is correct without a theme flash
(specification, 12.3, 12.4).

Accessibility rules this file is the single owner of (specification, 12.4):
every color is a token in one of the two ``:root`` blocks so the palette can be
checked numerically against WCAG 2.2 AA; every interactive element is reachable
by keyboard in document order and shows a focus outline; every state that
matters is carried by text and shape, never by color alone.
"""

import html

from glite_english_audit.artifacts.models import ReviewedRecord
from glite_english_audit.consent import CONSENT_POLICY_VERSION
from glite_english_audit.review_server.session import ReviewSessionState
from glite_english_audit.submission.capability import SubmissionCapability

__all__ = ["CONSENT_POLICY_VERSION", "mistake_noun", "render_page"]

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
PACKAGE_NOTE_TEXT = (
    "This JSON is byte for byte what Glite would receive. The box below "
    "scrolls sideways for long lines."
)
DOWNLOAD_LINK_TEXT = "Download the submission package"
DOWNLOAD_NOTE_TEXT = "The file is the exact JSON shown above."
SEND_REQUIREMENTS_TEXT = "Check both confirmations to send. At least one record must stay included."
SKIP_LINK_TEXT = "Skip to send or save"

_SKIP_TARGET_ID = "send-section"

_STYLE = """
:root {
  color-scheme: light dark;
  --bg: #FBFCFF;
  --ink: #020306;
  --ink-soft: #1E1F22;
  --line: rgba(2, 3, 6, 0.18);
  --line-strong: rgba(2, 3, 6, 0.45);
  --well: rgba(2, 3, 6, 0.05);
  --action: #005BFF;
  --action-text: #005BFF;
  --on-action: #FBFCFF;
  --focus: #005BFF;
  --ok: #0F7B3F;
  --fail: #C21F3A;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #020306;
    --ink: #FBFCFF;
    --ink-soft: rgba(251, 252, 255, 0.72);
    --line: rgba(251, 252, 255, 0.2);
    --line-strong: rgba(251, 252, 255, 0.38);
    --well: #1E1F22;
    --action-text: #7DA6FF;
    --focus: #7DA6FF;
    --ok: #4CC583;
    --fail: #FF7A8C;
  }
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
body {
  margin: 0 auto;
  padding: 1.5rem 1.25rem 4rem;
  max-width: 46rem;
  background: var(--bg);
  color: var(--ink);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  font-size: 1rem;
  line-height: 1.55;
}
h1 { font-size: 1.5rem; line-height: 1.3; margin: 0 0 0.5rem; }
h2 { font-size: 1.125rem; margin: 2rem 0 0.5rem; }
p { margin: 0.5rem 0; }
.muted { color: var(--ink-soft); }
.skip-link {
  position: absolute;
  left: -100vw;
  top: 0;
  z-index: 2;
  background: var(--bg);
  color: var(--action-text);
  border: 1px solid var(--action-text);
  border-radius: 6px;
  padding: 0.55rem 1.1rem;
  font-weight: 600;
  text-decoration: none;
}
.skip-link:focus { left: 0.75rem; top: 0.75rem; }
dl.summary { margin: 0.75rem 0; }
dl.summary .row {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: 0 1rem;
  padding: 0.1rem 0;
}
dl.summary dt { color: var(--ink-soft); }
dl.summary dd {
  margin: 0;
  text-align: right;
  font-variant-numeric: tabular-nums;
  overflow-wrap: anywhere;
}
ul.records { list-style: none; margin: 1rem 0; padding: 0; border-top: 1px solid var(--line); }
li.record { border-bottom: 1px solid var(--line); padding: 0.85rem 0; }
.record-include {
  display: flex;
  gap: 0.5rem;
  align-items: center;
  font-weight: 600;
  margin: 0 0 0.5rem;
}
dl.record-fields { margin: 0; }
dl.record-fields .row {
  display: grid;
  grid-template-columns: 7.5rem 1fr;
  gap: 0 1rem;
  padding: 0.1rem 0;
}
dl.record-fields dt { color: var(--ink-soft); font-size: 0.875rem; padding-top: 0.1rem; }
dl.record-fields dd { margin: 0; overflow-wrap: anywhere; }
@media (max-width: 30rem) {
  body { padding: 1.25rem 1rem 3rem; }
  dl.record-fields .row { grid-template-columns: 1fr; }
  dl.record-fields dd { margin-bottom: 0.35rem; }
  dl.summary .row { display: block; }
  dl.summary dd { text-align: left; }
  .button { width: 100%; }
}
input[type="checkbox"] {
  width: 1.5rem;
  height: 1.5rem;
  margin: 0;
  accent-color: var(--action);
  flex-shrink: 0;
}
.will-send { font-weight: 600; }
pre {
  background: var(--well);
  border: 1px solid var(--line-strong);
  border-radius: 6px;
  padding: 0.75rem;
  max-width: 100%;
  overflow-x: auto;
  font-size: 0.875rem;
  line-height: 1.45;
}
fieldset.confirmations {
  border: 1px solid var(--line-strong);
  border-radius: 6px;
  padding: 0.5rem 1rem 0.75rem;
  margin: 1rem 0;
  min-width: 0;
}
legend { font-weight: 600; padding: 0 0.35rem; }
.confirm-row { display: flex; gap: 0.6rem; align-items: flex-start; margin: 0.75rem 0; }
.confirm-row input { margin-top: 0.1rem; }
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
  text-align: center;
  max-width: 100%;
  text-decoration: none;
  cursor: pointer;
}
.button.primary {
  background: var(--action);
  color: var(--on-action);
  border: 1px solid var(--action);
}
.button.secondary {
  background: transparent;
  color: var(--action-text);
  border: 1px solid var(--action-text);
}
a.button.secondary:visited { color: var(--action-text); }
.button[aria-disabled="true"] {
  background: transparent;
  color: var(--ink-soft);
  border: 1px dashed var(--ink-soft);
  cursor: not-allowed;
}
#submit-status {
  margin: 1rem 0 0;
  padding-left: 0.6rem;
  border-left: 4px solid transparent;
  min-height: 1.55em;
}
.status-note { border-left-style: dotted; border-left-color: var(--ink-soft); font-weight: 600; }
.status-ok {
  border-left-style: solid;
  border-left-color: var(--ok);
  color: var(--ok);
  font-weight: 600;
}
.status-fail {
  border-left-style: dashed;
  border-left-color: var(--fail);
  color: var(--fail);
  font-weight: 600;
}
:focus-visible { outline: 3px solid var(--focus); outline-offset: 2px; }
@supports not selector(:focus-visible) {
  :focus { outline: 3px solid var(--focus); outline-offset: 2px; }
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
    scroll-behavior: auto !important;
  }
}
@media print {
  body { max-width: none; background: #FBFCFF; color: #020306; }
  .skip-link, .actions, #send-requirements, #submit-status { display: none; }
  pre { white-space: pre-wrap; overflow: visible; border-color: #020306; }
  ul.records, li.record, fieldset.confirmations { border-color: #020306; }
}
"""

_SCRIPT = """
(function () {
  "use strict";
  var token = document.body.getAttribute("data-token") || "";
  var sendButton = document.getElementById("send-button");
  var downloadLink = document.getElementById("download-link");
  var statusLine = document.getElementById("submit-status");
  var sent = false;

  function blocked(element) {
    return !element || element.getAttribute("aria-disabled") === "true";
  }

  function setBlocked(element, value) {
    if (element) { element.setAttribute("aria-disabled", value ? "true" : "false"); }
  }

  function setStatus(kind, message) {
    if (!statusLine) { return; }
    statusLine.className = kind;
    statusLine.textContent = message;
  }

  function setNoun(id, count) {
    // One record is a mistake, not "1 mistakes". The count and its noun change
    // together, so the noun is rewritten wherever the count is.
    var node = document.getElementById(id);
    if (node) { node.textContent = count === 1 ? "mistake" : "mistakes"; }
  }

  function applyCounts(data) {
    var willSend = document.getElementById("will-send-count");
    if (willSend) { willSend.textContent = String(data.will_send); }
    setNoun("will-send-noun", data.will_send);
    var sendCount = document.getElementById("send-count");
    if (sendCount) { sendCount.textContent = String(data.will_send); }
    setNoun("send-noun", data.will_send);
    var withheld = document.getElementById("withheld-user-count");
    if (withheld) { withheld.textContent = String(data.withheld_by_user); }
    setBlocked(downloadLink, data.will_send === 0);
    setBlocked(
      sendButton,
      sent || !(data.adult_confirmed && data.storage_confirmed && data.will_send > 0)
    );
  }

  function postDecision(payload) {
    return fetch("decisions", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Glite-Review": token },
      body: JSON.stringify(payload)
    }).then(function (response) {
      if (!response.ok) { throw new Error("decision refused"); }
      return response.json();
    }).then(applyCounts);
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
      setBlocked(downloadLink, false);
    }).catch(function () {
      if (view) { view.textContent = ""; view.parentElement.hidden = true; }
      if (empty) { empty.hidden = false; }
      setBlocked(downloadLink, true);
    });
  }

  function revert(box) {
    // The server owns the decision. A box that shows a change the server never
    // accepted would let the user send something they did not choose.
    box.checked = !box.checked;
    setStatus("status-fail", "Not saved. The change did not reach the local server.");
  }

  Array.prototype.forEach.call(
    document.querySelectorAll(".record-toggle"),
    function (box) {
      box.addEventListener("change", function () {
        postDecision({
          mistake_id: box.getAttribute("data-mistake-id"),
          included: box.checked
        }).then(refreshPackage).catch(function () { revert(box); });
      });
    }
  );

  Array.prototype.forEach.call(
    document.querySelectorAll(".confirm-toggle"),
    function (box) {
      box.addEventListener("change", function () {
        var payload = {};
        payload[box.getAttribute("data-confirm")] = box.checked;
        postDecision(payload).catch(function () { revert(box); });
      });
    }
  );

  if (downloadLink) {
    downloadLink.addEventListener("click", function (event) {
      if (blocked(downloadLink)) {
        event.preventDefault();
        setStatus("status-note", "Nothing to download. Include at least one record.");
      }
    });
  }

  if (sendButton) {
    sendButton.addEventListener("click", function () {
      if (sent) {
        setStatus("status-ok", "Sent. This package was already sent. Nothing was sent again.");
        return;
      }
      if (blocked(sendButton)) {
        setStatus(
          "status-note",
          "Not sent. Check both confirmations and keep at least one record."
        );
        return;
      }
      setBlocked(sendButton, true);
      setStatus("status-note", "Sending.");
      fetch("submit", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Glite-Review": token },
        body: "{}"
      }).then(function (response) {
        return response.json().then(function (data) {
          return { ok: response.ok, data: data };
        });
      }).then(function (result) {
        if (result.ok) {
          sent = true;
          setStatus("status-ok", "Sent. Submission ID: " + result.data.submission_id);
        } else {
          setStatus("status-fail", "Not sent. " + (result.data.reason || "Sending failed."));
          setBlocked(sendButton, false);
        }
      }).catch(function () {
        setStatus("status-fail", "Not sent. The request failed. Nothing was retried.");
        setBlocked(sendButton, false);
      });
    });
  }
})();
"""


def _escape(value: str) -> str:
    return html.escape(value, quote=True)


def mistake_noun(count: int) -> str:
    """The noun that agrees with a record count: singular only at one.

    The count is interpolated into two sentences the user acts on, and a tool
    that teaches English agreement cannot write "Send 1 mistakes anonymously".
    The page script applies the same rule when a checkbox changes the count.
    """
    return "mistake" if count == 1 else "mistakes"


def _definition_row(term: str, value: str, *, value_id: str | None = None) -> str:
    """One term/value pair wrapped in a div.

    The wrapper exists so the ``dl`` itself stays ``display: block``: applying
    grid or flex directly to a ``dl`` drops description-list semantics in some
    browsers.
    """
    attribute = f' id="{value_id}"' if value_id is not None else ""
    return f'<div class="row"><dt>{term}</dt><dd{attribute}>{value}</dd></div>'


def _record_row(index: int, total: int, entry: ReviewedRecord) -> str:
    record = entry.record
    checkbox_id = f"include-{index}"
    mistake_id = f"record-{index}-mistake"
    checked = " checked" if entry.included else ""
    fields = (
        _definition_row("Mistake", _escape(record.mistake), value_id=mistake_id)
        + _definition_row("Rule", _escape(record.rule))
        + _definition_row("Example", _escape(record.example))
        + _definition_row("Example type", _escape(record.example_type.value))
        + _definition_row("Source", _escape(record.source_type))
        + _definition_row("Modality", _escape(record.modality.value))
    )
    return (
        '<li class="record">'
        '<div class="record-include">'
        f'<input type="checkbox" id="{checkbox_id}" class="record-toggle" '
        f'data-mistake-id="{_escape(entry.mistake_id)}" '
        f'aria-describedby="{mistake_id}"{checked}> '
        f'<label for="{checkbox_id}">Include mistake {index + 1} of {total} '
        "in the submission</label>"
        "</div>"
        f'<dl class="record-fields">{fields}</dl>'
        "</li>"
    )


def _summary_section(state: ReviewSessionState) -> str:
    counts = state.counts
    rows = (
        _definition_row(
            "Eligible English words analyzed",
            f"{counts.analyzed_english_words} of {counts.eligible_english_words}",
        )
        + _definition_row(
            "Eligible messages analyzed",
            f"{counts.analyzed_utterances} of {counts.eligible_utterances}",
        )
        + _definition_row("Verified mistakes", str(counts.verified_total_mistakes))
        + _definition_row("Could not be made safe to share", str(counts.withheld_for_privacy))
        + _definition_row(
            "Withheld by you",
            str(counts.withheld_by_user),
            value_id="withheld-user-count",
        )
    )
    return (
        '<section aria-labelledby="summary-heading">'
        '<h2 id="summary-heading">What this audit covered</h2>'
        f'<dl class="summary">{rows}</dl>'
        "</section>"
    )


def _records_section(state: ReviewSessionState) -> str:
    records = state.records
    total = len(records)
    rows = "".join(_record_row(index, total, entry) for index, entry in enumerate(records))
    # role="list" is required because list-style: none drops list semantics in
    # WebKit; the count of records is the point of the list here.
    return (
        '<section aria-labelledby="records-heading">'
        f'<h2 id="records-heading">Mistake records ({total})</h2>'
        "<p>Each record below is shown exactly as it would be sent. "
        "You can include or exclude records; you cannot edit them.</p>"
        f"<p>{_escape(EXCLUSION_EXPLANATION_TEXT)}</p>"
        f'<ul class="records" role="list">{rows}</ul>'
        '<p class="will-send" id="will-send" aria-live="polite" aria-atomic="true">Will send '
        f'<span id="will-send-count">{state.included_count}</span> '
        f'<span id="will-send-noun">{mistake_noun(state.included_count)}</span>.</p>'
        "</section>"
    )


def _package_section(package_bytes: bytes | None) -> str:
    if package_bytes is None:
        body = ""
        pre_hidden = " hidden"
        empty_hidden = ""
    else:
        body = _escape(package_bytes.decode("utf-8"))
        pre_hidden = ""
        empty_hidden = " hidden"
    # The package box scrolls on its own, so it needs a role, a name, and a tab
    # stop; a scrollable box with none of those is unreachable by keyboard.
    return (
        '<section aria-labelledby="package-heading">'
        '<h2 id="package-heading">Exact submission package</h2>'
        f'<p id="package-note">{_escape(PACKAGE_NOTE_TEXT)}</p>'
        '<pre id="package-view" tabindex="0" role="region" '
        'aria-labelledby="package-heading" aria-describedby="package-note"'
        f"{pre_hidden}><code>{body}</code></pre>"
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
        '<div class="confirm-row">'
        '<input type="checkbox" id="adult-confirmed" class="confirm-toggle" '
        f'data-confirm="adult_confirmed"{adult_checked}> '
        f'<label for="adult-confirmed">{_escape(ADULT_CONFIRMATION_TEXT)}</label>'
        "</div>"
        '<div class="confirm-row">'
        '<input type="checkbox" id="storage-confirmed" class="confirm-toggle" '
        f'data-confirm="storage_confirmed"{storage_checked}> '
        f'<label for="storage-confirmed">{_escape(STORAGE_CONFIRMATION_TEXT)}</label>'
        "</div>"
        "</fieldset>"
    )


def _actions(
    state: ReviewSessionState,
    capability: SubmissionCapability,
    *,
    package_available: bool,
) -> str:
    download_blocked = "false" if package_available else "true"
    download = (
        '<a class="button secondary" id="download-link" href="package.json" '
        'type="application/json" download="glite-submission-package.json" '
        f'aria-describedby="download-note" aria-disabled="{download_blocked}">'
        f"{_escape(DOWNLOAD_LINK_TEXT)}</a>"
    )
    notes = f'<p id="download-note" class="muted">{_escape(DOWNLOAD_NOTE_TEXT)}</p>'
    if capability.direct_submission_available:
        # aria-disabled, not the disabled attribute: the button stays focusable
        # so a keyboard or screen-reader user can reach it and read why it is
        # blocked. The server refuses the submission independently.
        ready = state.adult_confirmed and state.storage_confirmed and state.included_count > 0
        send_blocked = "false" if ready else "true"
        action = (
            '<button type="button" class="button primary" id="send-button" '
            f'aria-describedby="send-requirements" aria-disabled="{send_blocked}">'
            f'Send <span id="send-count">{state.included_count}</span> '
            f'<span id="send-noun">{mistake_noun(state.included_count)}</span> '
            "anonymously</button>"
        )
        notes += f'<p id="send-requirements" class="muted">{_escape(SEND_REQUIREMENTS_TEXT)}</p>'
    else:
        action = ""
        notes += f'<p id="download-only-note">{_escape(DOWNLOAD_ONLY_TEXT)}</p>'
    return (
        f'<section id="{_SKIP_TARGET_ID}" tabindex="-1" aria-labelledby="send-heading">'
        '<h2 id="send-heading">Send or save</h2>'
        + _confirmations(state)
        + f'<div class="actions">{download}{action}</div>'
        + notes
        + '<p id="submit-status" role="status"></p>'
        "</section>"
    )


def render_page(
    state: ReviewSessionState,
    capability: SubmissionCapability,
    token: str,
) -> str:
    """Render the complete review page for the current session state."""
    package_bytes = state.current_package_bytes()
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
        f'<a class="skip-link" href="#{_SKIP_TARGET_ID}">{_escape(SKIP_LINK_TEXT)}</a>\n'
        "<main>\n"
        "<h1>Final review before anything leaves this computer</h1>\n"
        "<p>Nothing has been sent yet. Review each record below. "
        "Only what you see on this page can go to Glite.</p>\n"
        + _summary_section(state)
        + _records_section(state)
        + _package_section(package_bytes)
        + _actions(state, capability, package_available=package_bytes is not None)
        + "\n</main>\n"
        f"<script>{_SCRIPT}</script>\n"
        "</body>\n"
        "</html>\n"
    )

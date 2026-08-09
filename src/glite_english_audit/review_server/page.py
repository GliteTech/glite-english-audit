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
    "Nothing will be sent now. Download the package and upload it on the "
    "Glite website when you are ready."
)
EXCLUSION_EXPLANATION_TEXT = (
    "Uncheck any example you do not want to share. Its complete record will be "
    "removed, and the anonymous withheld count will increase by one."
)
PACKAGE_NOTE_TEXT = (
    "This JSON is byte for byte what Glite would receive. Long lines scroll sideways."
)
DOWNLOAD_LINK_TEXT = "Download package"
DOWNLOAD_NOTE_TEXT = "Downloads the same exact JSON available below."
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
  --well: #F1F4FA;
  --well-hover: #E8EDF7;
  --note-bg: #EFF4FF;
  --ok-bg: #EAF8F0;
  --fail-bg: #FFF0F2;
  --z-popover: 20;
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
    --well: #151820;
    --well-hover: #1E2430;
    --note-bg: #101B33;
    --ok-bg: #0B2518;
    --fail-bg: #2D1117;
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
  padding: 2rem 1.25rem 4rem;
  max-width: 52rem;
  background: var(--bg);
  color: var(--ink);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  font-size: 1rem;
  line-height: 1.55;
}
h1 {
  max-width: 25ch;
  margin: 0 0 0.5rem;
  font-size: 1.75rem;
  line-height: 1.2;
  letter-spacing: -0.02em;
  text-wrap: balance;
}
h2 { margin: 2rem 0 0.5rem; font-size: 1.125rem; line-height: 1.3; text-wrap: balance; }
p { margin: 0.5rem 0; }
.muted { color: var(--ink-soft); }
.intro { max-width: 66ch; color: var(--ink-soft); text-wrap: pretty; }
.visually-hidden {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
  border: 0;
}
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
dl.summary { margin: 0.65rem 0 0; }
dl.summary .row {
  display: inline-flex;
  gap: 0.35rem;
  margin: 0.15rem 1rem 0.15rem 0;
}
dl.summary dt { color: var(--ink-soft); }
dl.summary dd {
  margin: 0;
  font-weight: 650;
  font-variant-numeric: tabular-nums;
  overflow-wrap: anywhere;
}
ul.records {
  list-style: none;
  margin: 1rem 0 0;
  padding: 0;
  border-top: 1px solid var(--line);
}
li.record {
  position: relative;
  border-bottom: 1px solid var(--line);
  padding: 0.45rem 0;
}
.record-line {
  display: grid;
  grid-template-columns: 1.5rem minmax(0, 1fr) 2.75rem;
  gap: 0.75rem;
  align-items: start;
  min-height: 2.75rem;
}
.record-example {
  align-self: center;
  padding: 0.3rem 0;
  font-weight: 600;
  line-height: 1.4;
  overflow-wrap: anywhere;
  text-wrap: pretty;
  cursor: pointer;
}
.record:has(.record-toggle:not(:checked)) .record-example {
  color: var(--ink-soft);
  font-weight: 450;
}
.record-info {
  width: 2.75rem;
  height: 2.75rem;
  padding: 0;
  border: 1px solid var(--line-strong);
  border-radius: 50%;
  background: transparent;
  color: var(--action-text);
  font: inherit;
  font-weight: 750;
  cursor: pointer;
}
.record-info:hover,
.record-info[aria-expanded="true"] { background: var(--well-hover); }
.record-popover {
  position: absolute;
  z-index: var(--z-popover);
  top: calc(100% - 0.2rem);
  right: 0;
  width: min(34rem, calc(100vw - 2rem));
  padding: 1rem;
  border: 1px solid var(--line-strong);
  border-radius: 10px;
  background: var(--bg);
}
.popover-heading { margin: 0 0 0.55rem; font-weight: 700; }
dl.record-fields { margin: 0; }
dl.record-fields .row {
  display: grid;
  grid-template-columns: 7rem minmax(0, 1fr);
  gap: 0 0.8rem;
  padding: 0.2rem 0;
}
dl.record-fields dt { color: var(--ink-soft); font-size: 0.875rem; }
dl.record-fields dd { margin: 0; overflow-wrap: anywhere; }
input[type="checkbox"] {
  width: 1.5rem;
  height: 1.5rem;
  margin: 0.6rem 0 0;
  accent-color: var(--action);
  flex-shrink: 0;
}
.will-send { margin-top: 0.75rem; font-weight: 700; font-variant-numeric: tabular-nums; }
.package-disclosure { margin-top: 2rem; }
.package-disclosure summary {
  width: fit-content;
  color: var(--action-text);
  font-weight: 650;
  cursor: pointer;
}
.package-content { margin-top: 0.75rem; }
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
  border-radius: 8px;
  min-height: 1.55em;
}
.status-message { padding: 0.7rem 0.8rem; border: 1px solid; font-weight: 650; }
.status-note { border-color: var(--ink-soft); background: var(--note-bg); }
.status-ok {
  border-color: var(--ok);
  background: var(--ok-bg);
  color: var(--ok);
}
.status-fail {
  border-color: var(--fail);
  background: var(--fail-bg);
  color: var(--fail);
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
@media (max-width: 40rem) {
  body { padding: 1.25rem 1rem 3rem; }
  .record-line { grid-template-columns: 1.5rem minmax(0, 1fr) 2.75rem; gap: 0.6rem; }
  .record-popover {
    position: static;
    width: auto;
    margin: 0.35rem 0 0.4rem 2.1rem;
  }
  dl.record-fields .row { grid-template-columns: 1fr; }
  dl.record-fields dd { margin-bottom: 0.25rem; }
  .button { width: 100%; }
}
@media print {
  body { max-width: none; background: #FBFCFF; color: #020306; }
  .skip-link, .record-info, .record-popover, .actions,
  #send-requirements, #submit-status { display: none; }
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
  var disconnected = false;
  var openInfoButton = null;
  var closeInfoTimer = null;

  function blocked(element) {
    return !element || element.getAttribute("aria-disabled") === "true";
  }

  function setBlocked(element, value) {
    if (element) { element.setAttribute("aria-disabled", value ? "true" : "false"); }
  }

  function setStatus(kind, message) {
    if (!statusLine) { return; }
    statusLine.className = "status-message " + kind;
    statusLine.textContent = message;
  }

  function setDecisionControlsDisabled(value) {
    Array.prototype.forEach.call(
      document.querySelectorAll(".record-toggle, .confirm-toggle"),
      function (control) { control.disabled = value; }
    );
  }

  function markDisconnected(message) {
    disconnected = true;
    setDecisionControlsDisabled(true);
    setBlocked(downloadLink, true);
    setBlocked(sendButton, true);
    setStatus("status-fail", message);
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
    return data;
  }

  function postDecision(payload) {
    return fetch("decisions", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Glite-Review": token },
      body: JSON.stringify(payload)
    }).then(function (response) {
      if (!response.ok) { throw new Error("decision-refused"); }
      return response.json();
    }).then(applyCounts);
  }

  function showEmptyPackage(view, empty) {
    if (view) { view.textContent = ""; view.parentElement.hidden = true; }
    if (empty) { empty.hidden = false; }
    setBlocked(downloadLink, true);
  }

  function refreshPackage() {
    var view = document.querySelector("#package-view code");
    var empty = document.getElementById("package-empty");
    fetch("package.json").then(function (response) {
      if (response.status === 409) { return null; }
      if (!response.ok) { throw new Error("package-unavailable"); }
      return response.text();
    }).then(function (text) {
      if (text === null) { showEmptyPackage(view, empty); return; }
      if (view) { view.textContent = text; view.parentElement.hidden = false; }
      if (empty) { empty.hidden = true; }
      setBlocked(downloadLink, false);
    }).catch(function () {
      markDisconnected(
        "The local review server is no longer available. Restart the review command " +
        "and open its new 127.0.0.1 address."
      );
    });
  }

  function revert(box, error) {
    // The server owns the decision. A box that shows a change the server never
    // accepted would let the user send something they did not choose.
    box.checked = !box.checked;
    if (error && error.message === "decision-refused") {
      setStatus("status-fail", "Not saved. The local server refused this change.");
      return;
    }
    markDisconnected(
      "The local review server is no longer available. Restart the review command " +
      "and open its new 127.0.0.1 address."
    );
  }

  function infoPanel(button) {
    return document.getElementById(button.getAttribute("aria-controls"));
  }

  function closeInfo(button, force) {
    if (!button) { return; }
    if (!force && button.getAttribute("data-pinned") === "true") { return; }
    var panel = infoPanel(button);
    if (panel) { panel.hidden = true; }
    button.setAttribute("aria-expanded", "false");
    button.setAttribute("data-pinned", "false");
    if (openInfoButton === button) { openInfoButton = null; }
  }

  function showInfo(button, pin) {
    if (closeInfoTimer) { window.clearTimeout(closeInfoTimer); }
    if (openInfoButton && openInfoButton !== button) { closeInfo(openInfoButton, true); }
    var panel = infoPanel(button);
    if (panel) { panel.hidden = false; }
    button.setAttribute("aria-expanded", "true");
    if (pin) { button.setAttribute("data-pinned", "true"); }
    openInfoButton = button;
  }

  function scheduleInfoClose(button) {
    if (closeInfoTimer) { window.clearTimeout(closeInfoTimer); }
    closeInfoTimer = window.setTimeout(function () { closeInfo(button, false); }, 120);
  }

  Array.prototype.forEach.call(
    document.querySelectorAll(".record-info"),
    function (button) {
      var panel = infoPanel(button);
      button.addEventListener("mouseenter", function () { showInfo(button, false); });
      button.addEventListener("mouseleave", function () { scheduleInfoClose(button); });
      button.addEventListener("focus", function () { showInfo(button, false); });
      button.addEventListener("blur", function () { scheduleInfoClose(button); });
      button.addEventListener("click", function () {
        if (button.getAttribute("data-pinned") === "true") {
          closeInfo(button, true);
        } else {
          showInfo(button, true);
        }
      });
      if (panel) {
        panel.addEventListener("mouseenter", function () {
          if (closeInfoTimer) { window.clearTimeout(closeInfoTimer); }
        });
        panel.addEventListener("mouseleave", function () { scheduleInfoClose(button); });
      }
    }
  );

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && openInfoButton) {
      var button = openInfoButton;
      closeInfo(button, true);
      button.focus();
    }
  });

  document.addEventListener("pointerdown", function (event) {
    if (!openInfoButton) { return; }
    var record = openInfoButton.closest(".record");
    if (record && !record.contains(event.target)) { closeInfo(openInfoButton, true); }
  });

  Array.prototype.forEach.call(
    document.querySelectorAll(".record-toggle"),
    function (box) {
      box.addEventListener("change", function () {
        if (disconnected) { return; }
        postDecision({
          mistake_id: box.getAttribute("data-mistake-id"),
          included: box.checked
        }).then(function () { refreshPackage(); }).catch(function (error) { revert(box, error); });
      });
    }
  );

  Array.prototype.forEach.call(
    document.querySelectorAll(".confirm-toggle"),
    function (box) {
      box.addEventListener("change", function () {
        if (disconnected) { return; }
        var payload = {};
        payload[box.getAttribute("data-confirm")] = box.checked;
        postDecision(payload).catch(function (error) { revert(box, error); });
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
        markDisconnected(
          "Not sent. The local review server is no longer available. Restart the review " +
          "command and open its new 127.0.0.1 address. Nothing was retried."
        );
      });
    });
  }

  if (window.location.protocol === "file:" || !token ||
      token === "STATIC-SNAPSHOT-NO-LIVE-TOKEN") {
    markDisconnected(
      "This saved copy is read-only. Restart the review command and open the new " +
      "127.0.0.1 address it prints."
    );
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
    example_id = f"record-{index}-example"
    info_id = f"record-{index}-info"
    details_id = f"record-{index}-details"
    heading_id = f"record-{index}-details-heading"
    checked = " checked" if entry.included else ""
    fields = (
        _definition_row("Mistake", _escape(record.mistake))
        + _definition_row("Rule", _escape(record.rule))
        + _definition_row("Example", _escape(record.example))
        + _definition_row("Example type", _escape(record.example_type.value))
        + _definition_row("Source", _escape(record.source_type))
        + _definition_row("Modality", _escape(record.modality.value))
    )
    return (
        '<li class="record">'
        '<div class="record-line">'
        f'<input type="checkbox" id="{checkbox_id}" class="record-toggle" '
        f'data-mistake-id="{_escape(entry.mistake_id)}" '
        f"{checked}>"
        f'<label class="record-example" id="{example_id}" for="{checkbox_id}">'
        f'<span class="visually-hidden">Include mistake {index + 1} of {total}: </span>'
        f"{_escape(record.example)}</label>"
        f'<button type="button" class="record-info" id="{info_id}" '
        f'aria-label="Show all fields for mistake {index + 1} of {total}" '
        f'aria-expanded="false" aria-controls="{details_id}" data-pinned="false">'
        '<span aria-hidden="true">i</span></button>'
        "</div>"
        f'<div class="record-popover" id="{details_id}" role="region" '
        f'aria-labelledby="{heading_id}" hidden>'
        f'<p class="popover-heading" id="{heading_id}">Mistake {index + 1} of {total}</p>'
        f'<dl class="record-fields">{fields}</dl>'
        "</div>"
        "</li>"
    )


def _summary_section(state: ReviewSessionState) -> str:
    counts = state.counts
    rows = (
        _definition_row(
            "Words analyzed",
            f"{counts.analyzed_english_words} of {counts.eligible_english_words}",
        )
        + _definition_row(
            "Messages analyzed",
            f"{counts.analyzed_utterances} of {counts.eligible_utterances}",
        )
        + _definition_row("Verified mistakes", str(counts.verified_total_mistakes))
        + _definition_row("Withheld for privacy", str(counts.withheld_for_privacy))
        + _definition_row(
            "Excluded by you",
            str(counts.withheld_by_user),
            value_id="withheld-user-count",
        )
    )
    return (
        '<section aria-labelledby="summary-heading">'
        '<h2 id="summary-heading">Audit summary</h2>'
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
        f'<h2 id="records-heading">Mistakes to include ({total})</h2>'
        "<p>Each checked sentence is the privacy-safe example Glite will receive. "
        "Examples may be synthetic. Use the info button to see the complete record.</p>"
        f"<p>{_escape(EXCLUSION_EXPLANATION_TEXT)}</p>"
        f'<ul class="records" role="list">{rows}</ul>'
        '<p class="will-send" id="will-send" aria-live="polite" aria-atomic="true">'
        f'<span id="will-send-count">{state.included_count}</span> of {total} selected.</p>'
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
        '<section class="package-disclosure" aria-label="Exact submission package">'
        '<details><summary id="package-heading">View exact submission JSON</summary>'
        '<div class="package-content">'
        f'<p id="package-note">{_escape(PACKAGE_NOTE_TEXT)}</p>'
        '<pre id="package-view" tabindex="0" role="region" '
        'aria-labelledby="package-heading" aria-describedby="package-note"'
        f"{pre_hidden}><code>{body}</code></pre>"
        f'<p id="package-empty" class="muted"{empty_hidden}>'
        "Every record is excluded, so there is no package to send.</p>"
        "</div></details></section>"
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
        confirmations = _confirmations(state)
        heading = "Send or save"
    else:
        action = ""
        notes += f'<p id="download-only-note">{_escape(DOWNLOAD_ONLY_TEXT)}</p>'
        confirmations = ""
        heading = "Save package"
    return (
        f'<section id="{_SKIP_TARGET_ID}" tabindex="-1" aria-labelledby="send-heading">'
        f'<h2 id="send-heading">{heading}</h2>'
        + confirmations
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
        "<h1>Choose which mistakes to share</h1>\n"
        '<p class="intro">Nothing has been sent. Review the checked examples below. '
        "Only their privacy-safe records can go to Glite.</p>\n"
        + _summary_section(state)
        + _records_section(state)
        + _actions(state, capability, package_available=package_bytes is not None)
        + _package_section(package_bytes)
        + "\n</main>\n"
        f"<script>{_SCRIPT}</script>\n"
        "</body>\n"
        "</html>\n"
    )

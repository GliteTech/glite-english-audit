"""Server-rendered HTML for the final local review page.

One self-contained document: a single inline style block, a small inline
script, loopback requests for current state, and one deliberate native form
handoff to the fixed Glite report website. Light and dark themes are both
first-class through CSS custom properties and
``prefers-color-scheme``, so the first paint is correct without a theme flash
(specification, 12.3, 12.4).

Accessibility rules this file is the single owner of (specification, 12.4):
every color is a token in one of the two ``:root`` blocks so the palette can be
checked numerically against WCAG 2.2 AA; every interactive element is reachable
by keyboard in document order and shows a focus outline; every state that
matters is carried by text and shape, never by color alone.
"""

import html

from glite_english_audit.artifacts.enums import ExampleType
from glite_english_audit.artifacts.models import ReviewedRecord
from glite_english_audit.consent import CONSENT_POLICY_VERSION
from glite_english_audit.review_server.report_handoff import REPORT_PAGE_URL
from glite_english_audit.review_server.session import ReviewSessionState
from glite_english_audit.submission.capability import SubmissionCapability

__all__ = ["CONSENT_POLICY_VERSION", "mistake_noun", "render_page"]

ADULT_CONFIRMATION_TEXT = "I confirm that I am at least 18 years old."
STORAGE_CONFIRMATION_TEXT = (
    "I understand and accept all of the following. Glite keeps the records I "
    "send permanently and cannot delete them later. Glite uses them to run and "
    "improve the product, to train models, and in research that combines many "
    "people's records. Glite sends them to an outside AI model to write my "
    "report and flashcards."
)
"""One agreement, said as four short sentences instead of one long clause.

It was a single 44-word sentence joining four separate commitments with
semicolons, every one of them a noun rather than a verb -- "permanent,
irrevocable storage", "the disclosed product, knowledge-graph, model-training,
and aggregate-research uses", "external AI processing". "Disclosed" pointed at a
disclosure the reader could not see from here.

The specification forbids exactly this: consent items are not bundled, and
consent text stays literal. This is the sentence someone reads at the moment
they click, and it is the last one that should need a second pass.
"""
DOWNLOAD_FALLBACK_TEXT = "Not now? Download the package and upload it whenever you like."

PRIVACY_POLICY_URL = "https://glite.ai/policies/privacy"
TERMS_URL = "https://glite.ai/policies/terms-and-conditions"
"""Where the agreement beside the checkbox can actually be read.

The storage confirmation asks for permanent storage, model training and external
AI processing, and until now the page carried no way to read any of it. The
sentence was the entire disclosure. An earlier version pointed at "the disclosed
uses" without saying where they were disclosed, which was worse; making the
sentence honest was only half the fix, because a person who wants the detail
still had nowhere to go.

Absolute URLs on the production domain, not the test host: test.glite.ai is a
single-page app with no policy route at all, and every legal path there answers
404.
"""

WHAT_YOU_GET_TEXT = (
    "Send these and Glite groups them into the mistakes you make most, with a rule "
    "and an example for each. The report is yours to keep."
)
"""The reason to do any of this, which the page never gave.

Everything here described a cost -- permanent storage, model training, an outside
model reading it -- and nothing described what the cost buys. A reader arriving
at this page knew they had thirty-six sentences and two consent boxes, and had to
infer the point from a button label.
"""
EXAMPLE_ORIGIN_LABELS: dict[ExampleType, str] = {
    ExampleType.VERBATIM: "your words",
    ExampleType.REDACTED: "your words, changed",
    ExampleType.SYNTHETIC: "invented",
}
"""How each example's provenance reads on the row you decide from.

``example_type`` has always been in the record and has always been one click
away in the info popover. It belongs on the row now because it stopped being
uniform: an example may be the sentence you wrote. Consenting to send your own
writing and consenting to send an invented sentence carrying the same error are
different decisions, and the page that asks for consent has to say which one it
is asking for without being opened first.
"""
PACKAGE_NOTE_TEXT = (
    "This JSON is byte for byte what Glite would receive. Long lines scroll sideways."
)
DOWNLOAD_LINK_TEXT = "Download package"
SEND_REQUIREMENTS_TEXT = "Check both boxes above, and keep at least one mistake."
REPORT_REQUIREMENTS_TEXT = "Check both boxes above, and keep at least one mistake."
SKIP_LINK_TEXT = "Skip to the send and download buttons"
ACTION_BAR_LINK_TEXT = "Create report or download"
"""The way out of a list that can be long enough to hide its own ending.

Thirty-six records is several screens, and the only thing a reader can actually
do sits under all of them. Nothing on the first screen said so, so the page
looked like a list to read rather than a decision to make.

The bar sticks to the bottom of the viewport carrying the count that already
changes as boxes are ticked, so the number is visible while the ticking happens
rather than only once the reader arrives at the end.

It links rather than submits. Sending needs two confirmations that live in the
actions section, and a button here would either duplicate them or bypass them --
one is a maintenance trap and the other is a consent bug."""

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
.record-text {
  align-self: center;
  min-width: 0;
}
.record-example {
  display: block;
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
.record-origin {
  display: block;
  color: var(--ink-soft);
  font-size: 0.85rem;
  line-height: 1.3;
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
.legal-links {
  margin: 0.6rem 0 0;
  font-size: 0.9rem;
  color: var(--ink-soft);
}
.legal-links a { color: var(--action-text); }
.action-bar {
  position: sticky;
  bottom: 0;
  z-index: 10;
  display: flex;
  flex-wrap: wrap;
  gap: 0.75rem 1.25rem;
  align-items: center;
  justify-content: space-between;
  margin: 1.5rem -1.25rem 0;
  padding: 0.85rem 1.25rem;
  background: var(--bg);
  border-top: 1px solid var(--line-strong);
}
.action-bar .count {
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.action-bar a {
  color: var(--action-text);
  font-weight: 650;
  text-decoration: none;
  border: 1px solid var(--action-text);
  border-radius: 6px;
  padding: 0.5rem 1rem;
}
.action-bar a:hover { background: var(--well-hover); }
@media (prefers-reduced-motion: no-preference) {
  html { scroll-behavior: smooth; }
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
.report-form { margin: 0; }
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
  .report-form { width: 100%; }
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
  var reportForm = document.getElementById("report-form");
  var reportButton = document.getElementById("report-button");
  var reportSubmission = document.getElementById("report-submission");
  var reportConfirmationAt = document.getElementById("report-confirmation-at");
  var downloadLink = document.getElementById("download-link");
  var statusLine = document.getElementById("submit-status");
  var sent = false;
  var disconnected = false;
  var packageReady = !!(reportSubmission && reportSubmission.value);
  var latestCounts = null;
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
    setBlocked(reportButton, true);
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
    latestCounts = data;
    var willSend = document.getElementById("will-send-count");
    if (willSend) { willSend.textContent = String(data.will_send); }
    var sendCount = document.getElementById("send-count");
    if (sendCount) { sendCount.textContent = String(data.will_send); }
    setNoun("send-noun", data.will_send);
    var withheld = document.getElementById("withheld-user-count");
    if (withheld) { withheld.textContent = String(data.withheld_by_user); }
    setBlocked(downloadLink, !packageReady || data.will_send === 0);
    setBlocked(
      reportButton,
      !packageReady ||
        !(data.adult_confirmed && data.storage_confirmed && data.will_send > 0)
    );
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
    if (reportSubmission) { reportSubmission.value = ""; }
    packageReady = false;
    setBlocked(downloadLink, true);
    setBlocked(reportButton, true);
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
      if (reportSubmission) { reportSubmission.value = text; }
      packageReady = true;
      if (latestCounts) { applyCounts(latestCounts); }
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
        }).then(function () {
          packageReady = false;
          setBlocked(downloadLink, true);
          setBlocked(reportButton, true);
          refreshPackage();
        }).catch(function (error) { revert(box, error); });
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

  if (reportForm) {
    reportForm.addEventListener("submit", function (event) {
      if (blocked(reportButton) || !reportSubmission || !reportSubmission.value) {
        event.preventDefault();
        setStatus(
          "status-note",
          "Report not created. Check both confirmations and keep at least one record."
        );
        return;
      }
      if (reportConfirmationAt) { reportConfirmationAt.value = new Date().toISOString(); }
      setBlocked(reportButton, true);
      setStatus("status-note", "Opening your report on the Glite website.");
    });
  }

  if (sendButton) {
    sendButton.addEventListener("click", function () {
      if (sent) {
        setStatus("status-ok", "Already sent. Nothing was sent again.");
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
    origin = EXAMPLE_ORIGIN_LABELS[record.example_type]
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
        '<div class="record-text">'
        f'<label class="record-example" id="{example_id}" for="{checkbox_id}">'
        f'<span class="visually-hidden">Include mistake {index + 1} of {total}: </span>'
        f"{_escape(record.example)}</label>"
        f'<span class="record-origin">{_escape(origin)}</span>'
        "</div>"
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


def _analyzed(analyzed: int, eligible: int) -> str:
    """ "2,183" when everything was read, "1,900 of 2,183" when some was not.

    The pair was always printed, so the ordinary run -- where the audit reads
    everything it found -- said "2183 of 2183". A number repeated against itself
    reads as a ratio worth checking and turns out to carry no information, which
    costs the reader more than the shortfall it exists to disclose.
    """
    if analyzed >= eligible:
        return f"{analyzed:,}"
    return f"{analyzed:,} of {eligible:,}"


def _summary_section(state: ReviewSessionState) -> str:
    counts = state.counts
    rows = (
        _definition_row(
            "Your words checked",
            _analyzed(counts.analyzed_english_words, counts.eligible_english_words),
        )
        + _definition_row("Mistakes found", str(counts.verified_total_mistakes))
        + _definition_row("Held back for privacy", str(counts.withheld_for_privacy))
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
        "<p>Each sentence below is exactly what Glite would receive, and each says "
        "whether it is your words, your words with a detail changed, or invented. "
        "Unchecking one sends no part of it.</p>"
        f'<ul class="records" role="list">{rows}</ul>'
        '<div class="action-bar">'
        '<p class="count will-send" id="will-send" aria-live="polite" aria-atomic="true">'
        f'<span id="will-send-count">{state.included_count}</span> of {total} selected.</p>'
        f'<a id="action-bar-link" href="#{_SKIP_TARGET_ID}">{_escape(ACTION_BAR_LINK_TEXT)}</a>'
        "</div>"
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
        "<legend>Both are required</legend>"
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
        '<p class="legal-links">'
        f'<a href="{_escape(PRIVACY_POLICY_URL)}" target="_blank" rel="noopener noreferrer">'
        "Privacy Policy</a> · "
        f'<a href="{_escape(TERMS_URL)}" target="_blank" rel="noopener noreferrer">'
        "Terms and Conditions</a>"
        "</p>"
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
        f'aria-disabled="{download_blocked}">'
        f"{_escape(DOWNLOAD_LINK_TEXT)}</a>"
    )
    ready = state.adult_confirmed and state.storage_confirmed and state.included_count > 0
    report_blocked = "false" if ready else "true"
    package_text = ""
    if package_available:
        package_bytes = state.current_package_bytes()
        if package_bytes is not None:
            package_text = package_bytes.decode("utf-8")
    report = (
        f'<form class="report-form" id="report-form" action="{_escape(REPORT_PAGE_URL)}" '
        'method="post" accept-charset="UTF-8">'
        '<input type="hidden" id="report-submission" name="submission" '
        f'value="{_escape(package_text)}">'
        '<input type="hidden" name="adult_attested" value="true">'
        '<input type="hidden" name="permanent_storage_and_uses_accepted" value="true">'
        '<input type="hidden" name="external_ai_processing_accepted" value="true">'
        f'<input type="hidden" name="consent_policy_version" '
        f'value="{_escape(CONSENT_POLICY_VERSION)}">'
        '<input type="hidden" id="report-confirmation-at" '
        'name="client_confirmation_at" value="">'
        '<button type="submit" class="button primary" id="report-button" '
        f'aria-describedby="report-requirements" aria-disabled="{report_blocked}">'
        "Create report</button>"
        "</form>"
    )
    # One helper line, not three. "Downloads the same exact JSON available
    # below" described a link sitting beside it, and the requirements sentence
    # restated two unchecked boxes the reader can see. What survives is the
    # blocking condition and the fallback, which are the two things not visible
    # from the controls themselves.
    notes = (
        f'<p id="report-requirements" class="muted">{_escape(REPORT_REQUIREMENTS_TEXT)}</p>'
        f'<p id="download-fallback-note" class="muted">{_escape(DOWNLOAD_FALLBACK_TEXT)}</p>'
    )
    if capability.direct_submission_available:
        # aria-disabled, not the disabled attribute: the button stays focusable
        # so a keyboard or screen-reader user can reach it and read why it is
        # blocked. The server refuses the submission independently.
        send_blocked = "false" if ready else "true"
        action = (
            '<button type="button" class="button secondary" id="send-button" '
            f'aria-describedby="send-requirements" aria-disabled="{send_blocked}">'
            f'Send <span id="send-count">{state.included_count}</span> '
            f'<span id="send-noun">{mistake_noun(state.included_count)}</span> '
            "anonymously</button>"
        )
        notes += f'<p id="send-requirements" class="muted">{_escape(SEND_REQUIREMENTS_TEXT)}</p>'
        heading = "Create your report"
    else:
        action = ""
        heading = "Create your report"
    return (
        f'<section id="{_SKIP_TARGET_ID}" tabindex="-1" aria-labelledby="send-heading">'
        f'<h2 id="send-heading">{heading}</h2>'
        + _confirmations(state)
        + f'<div class="actions">{report}{download}{action}</div>'
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
        "<title>Choose what to send \u2014 Glite</title>\n"
        f"<style>{_STYLE}</style>\n"
        "</head>\n"
        f'<body data-token="{_escape(token)}">\n'
        f'<a class="skip-link" href="#{_SKIP_TARGET_ID}">{_escape(SKIP_LINK_TEXT)}</a>\n'
        "<main>\n"
        "<h1>Choose which mistakes to share</h1>\n"
        f'<p class="intro">{_escape(WHAT_YOU_GET_TEXT)} Nothing has been sent yet — '
        "uncheck anything you would rather keep.</p>\n"
        + _summary_section(state)
        + _records_section(state)
        + _actions(state, capability, package_available=package_bytes is not None)
        + _package_section(package_bytes)
        + "\n</main>\n"
        f"<script>{_SCRIPT}</script>\n"
        "</body>\n"
        "</html>\n"
    )

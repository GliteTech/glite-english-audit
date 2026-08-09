# Website report handoff

Status: implementation specification for issue #6.

This document specifies how the final local review page hands the user's current package to the
Glite website and how that handoff remains consistent with the version-1 submission and consent
contracts.

## 1. Research record

Checked 2026-08-09:

- `GliteTech/glite-english-audit-website`, commit `88becc5`, `api/report-page.js`: the report page
  accepts `POST /report` as `application/x-www-form-urlencoded`; its `submission` field is the
  JSON text of one version-1 `SubmissionPackage`; a successful response is an HTML report.
- `https://glite-english-audit-website-eta.vercel.app/report`: the Git-managed production route
  accepts that form contract. `GET` is not a submission route.
- `src/glite_english_audit/review_server/page.py`: the local page already owns the current package
  bytes, include decisions, and the two required unchecked confirmations.
- `src/glite_english_audit/review_server/server.py`: the page's content security policy currently
  limits network requests to the loopback origin and needs an explicit fixed `form-action` target.
- `specifications/submission_contract.md` and `specifications/privacy_model.md`: consent stays
  outside the reusable package, and a new website submission requires the adult, permanent-use,
  and external-AI affirmations.

Confirmed behavior has one mismatch outside this repository: the current website handler reads
the package but does not yet reject missing consent fields. The local page still sends and enforces
its side of the contract. Website-side enforcement remains required before the website milestone
is complete.

## 2. Transport contract

The page renders one native HTML form with:

- `method="post"`;
- `action="https://glite-english-audit-website-eta.vercel.app/report"`;
- the browser's default `application/x-www-form-urlencoded` encoding;
- a hidden `submission` field whose value is the exact current package JSON;
- hidden affirmative consent fields outside the package;
- the current consent-policy version; and
- a client confirmation timestamp written immediately before submission.

The browser navigates in the same tab. The successful HTML response therefore becomes the page the
user sees. JavaScript does not read a cross-origin response, and package JSON, the recovery secret,
the local review token, and consent evidence never enter a URL.

The report origin is a source constant, not operator input. The content security policy permits
forms to that exact HTTPS origin and no other external origin.

## 3. User and state rules

`Create report` is the primary action. `Download package` remains a secondary fallback. Existing
direct API submission, when configured, remains a separate action.

The two confirmations start unchecked. The page blocks report creation until:

- the adult confirmation is checked;
- the permanent-storage/use and external-AI confirmation is checked;
- at least one record remains included; and
- the loopback review server is connected.

After every include or exclude decision, the page fetches `package.json` from the loopback server.
The exact response text updates both the visible JSON disclosure and the hidden `submission` field.
When every record is excluded, or the local server disconnects, both report creation and download
are blocked. A failed handoff is never retried in the background; resubmission requires another
explicit user action.

## 4. Test matrix

| Case | Required result |
|---|---|
| Initial page | Form carries the exact package bytes and both confirmations are unchecked. |
| Form contract | Fixed HTTPS action, POST method, `submission` field, consent fields, and no URL data. |
| One record excluded | Hidden package value is replaced from the refreshed local package. |
| Every record excluded | Create report and download are blocked; no useful package is present. |
| One or both confirmations missing | Create report is blocked and submission is prevented. |
| Local server disconnected | Decision controls, Create report, and download are blocked. |
| Direct capability available | Existing direct-send behavior remains independently gated. |
| Content security policy | Forms may target only the fixed report origin; fetch remains loopback-only. |
| Accessibility | Focus order, labels, blocked-state explanation, keyboard access, and live status remain valid. |
| Saved static copy | Create report is blocked because its embedded package can no longer follow decisions. |
